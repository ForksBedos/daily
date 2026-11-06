"""Командная строка: python -m uzdigest <команда>."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from uzdigest import __version__, service
from uzdigest.config import Settings, load_settings, load_sources
from uzdigest.db import repo
from uzdigest.dedup.report import write_clusters, write_pairs
from uzdigest.dedup.stage import prepare_window
from uzdigest.filter.prefilter import Keywords
from uzdigest.legal.doc_key import split_doc_key
from uzdigest.legal.lex_doc import act_link
from uzdigest.legal.verify import LexVerifier, verify_window
from uzdigest.llm.client import LlmBudget, LlmError, ask_json
from uzdigest.llm.draft import write_draft
from uzdigest.llm.stage import process_window
from uzdigest.metrics import render_text
from uzdigest.ops import log_file, setup_logging
from uzdigest.pipeline import rerender
from uzdigest.posts.generate import MODES, PostError
from uzdigest.render.model import LANGS
from uzdigest.timeutil import TASHKENT, tashkent_date, tashkent_to_utc, to_iso, utc_now
from uzdigest.web.http import HttpClient


def main(argv: list[str] | None = None) -> int:
    # Консоль Windows по умолчанию не в UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="uzdigest", description=f"UzDigest {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    collect_cmd = commands.add_parser(
        "collect", help="опросить источники, догрузить пробелы, скачать полный текст и курсы ЦБ"
    )
    collect_cmd.add_argument("--source", action="append", metavar="CODE", help="только этот источник")
    collect_cmd.add_argument("--no-backfill", action="store_true", help="без догрузки из sitemap и архивов")
    collect_cmd.add_argument("--no-enrich", action="store_true", help="без скачивания полного текста")
    collect_cmd.add_argument(
        "--since",
        type=_tashkent_time,
        metavar="ГГГГ-ММ-ДДTЧЧ:ММ",
        help="догрузить пробел с этого момента (время ташкентское), а не с последнего опроса",
    )
    commands.add_parser("sources-check", help="состояние источников")
    dedup_cmd = commands.add_parser(
        "dedup-report", help="кластеры и пары-кандидаты в дубли за окно → data/dedup_*.{csv,md}"
    )
    dedup_cmd.add_argument("--days", type=float, default=3, help="окно в сутках до текущего момента")
    verify_cmd = commands.add_parser(
        "verify-laws", help="сверить упоминания актов в материалах окна с lex.uz и показать статусы"
    )
    verify_cmd.add_argument("--days", type=float, default=3, help="окно в сутках до текущего момента")
    verify_cmd.add_argument(
        "--doc-key",
        action="append",
        metavar="KEY",
        help="только этот акт, например UP-236-2026 или MJ-1010-2",
    )
    draft_cmd = commands.add_parser(
        "draft",
        help="черновик дайджеста: сверка законов, отбор, тексты и перевод Claude за окно → data/draft/",
    )
    draft_cmd.add_argument("--days", type=float, default=1, help="окно в сутках до текущего момента")
    draft_cmd.add_argument("--no-llm", action="store_true", help="без Claude: проверить резервный режим")
    digest_cmd = commands.add_parser(
        "digest", help="дайджест дня: окно с прошлого дайджеста, отбор, тексты, перевод → outputs/ГГГГ-ММ-ДД/"
    )
    run_cmd = commands.add_parser("run", help="collect + digest: точка входа для Планировщика")
    for cmd in (digest_cmd, run_cmd):
        cmd.add_argument(
            "--force", action="store_true", help="пересобрать дайджест дня с новостями, вышедшими после него"
        )
        cmd.add_argument("--no-llm", action="store_true", help="без Claude: резервный режим")
    rerender_cmd = commands.add_parser("rerender", help="заново записать файлы дня из БД, без сети и Claude")
    rerender_cmd.add_argument("date", type=date.fromisoformat, metavar="ГГГГ-ММ-ДД")
    topics_cmd = commands.add_parser("topics", help="темы для постов: статьи дайджеста с номерами")
    post_cmd = commands.add_parser(
        "post", help="пост для соцсети по статье дайджеста → консоль и outputs/ДАТА/posts/"
    )
    post_cmd.add_argument("article", metavar="СТАТЬЯ", help="номер из uzdigest topics (5, 005) или полный id")
    post_cmd.add_argument(
        "--platform", required=True, help="площадка из config/posts.yaml: telegram, linkedin, …"
    )
    post_cmd.add_argument("--lang", choices=LANGS, help="язык поста; по умолчанию — первый язык площадки")
    post_cmd.add_argument(
        "--mode",
        choices=MODES,
        default="new",
        help="new — тот же пост из кэша, если он уже был; остальные — новый",
    )
    post_cmd.add_argument("--author", help="кто заказал пост: пишется в БД")
    for cmd in (topics_cmd, post_cmd):
        cmd.add_argument(
            "--date",
            type=date.fromisoformat,
            metavar="ГГГГ-ММ-ДД",
            help="дайджест за день; по умолчанию — последний",
        )
    status_cmd = commands.add_parser("status", help="что собрано, что не отвечает, Claude, следующий запуск")
    status_cmd.add_argument("--json", action="store_true", help="для машинного разбора")
    metrics_cmd = commands.add_parser(
        "metrics", help="показатели: надёжность, путь материалов, источники, Claude, посты, что повторяется"
    )
    metrics_cmd.add_argument("--days", type=int, default=30, help="за сколько дней считать")
    metrics_cmd.add_argument("--json", action="store_true", help="для машинного разбора")
    feedback_cmd = commands.add_parser("feedback", help="замечания команды к дайджесту и постам")
    feedback_actions = feedback_cmd.add_subparsers(dest="action", required=True)
    feedback_add = feedback_actions.add_parser("add", help="записать замечание")
    feedback_add.add_argument(
        "kind",
        choices=list(service.FEEDBACK_KINDS),
        help="missed — пропущено, duplicate — дубль, extra — лишнее, error — ошибка, "
        "translation — перевод, post — пост, other — прочее",
    )
    feedback_add.add_argument("text", help="текст замечания")
    feedback_add.add_argument("--article", help="id статьи: 2026-11-03-002")
    feedback_add.add_argument("--author", help="кто сделал замечание")
    feedback_list = feedback_actions.add_parser("list", help="замечания, по умолчанию все")
    feedback_export = feedback_actions.add_parser("export", help="выгрузка в CSV для Excel")
    feedback_export.add_argument("path", help="куда сохранить, например feedback.csv")
    for cmd in (feedback_add, feedback_list, feedback_export):
        cmd.add_argument("--date", type=date.fromisoformat, metavar="ГГГГ-ММ-ДД", help="дайджест за день")
    commands.add_parser("bot", help="Telegram-бот: работает, пока не остановят (Ctrl+C)")
    commands.add_parser("llm-check", help="тестовый вызов Claude по токену подписки")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.logs_dir, log_file(args.command))

    if args.command == "collect":
        only = set(args.source) if args.source else None
        return _collect(
            settings, only, backfill=not args.no_backfill, enrich_text=not args.no_enrich, gap_from=args.since
        )
    if args.command == "sources-check":
        return _sources_check(settings)
    if args.command == "dedup-report":
        return _dedup_report(settings, args.days)
    if args.command == "verify-laws":
        return _verify_laws(settings, args.days, args.doc_key or [])
    if args.command == "draft":
        return _draft(settings, args.days, use_llm=not args.no_llm)
    if args.command == "digest":
        return _digest(settings, force=args.force, use_llm=not args.no_llm)
    if args.command == "run":
        return _print_run(service.run("run", force=args.force, use_llm=not args.no_llm, settings=settings))
    if args.command == "status":
        return _status(settings, args.json)
    if args.command == "metrics":
        return _metrics(settings, args.days, args.json)
    if args.command == "feedback":
        return _feedback(settings, args)
    if args.command == "bot":
        return _bot(settings)
    if args.command == "rerender":
        return _rerender(settings, args.date)
    if args.command == "topics":
        return _topics(settings, args.date)
    if args.command == "post":
        return _post(settings, args)
    return _llm_check(settings)


def _tashkent_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment.astimezone(UTC) if moment.tzinfo else tashkent_to_utc(moment)


def _collect(
    settings: Settings,
    only: set[str] | None,
    *,
    backfill: bool,
    enrich_text: bool,
    gap_from: datetime | None = None,
) -> int:
    try:
        result = service.run(
            "collect",
            only=only,
            backfill=backfill,
            enrich_text=enrich_text,
            gap_from=gap_from,
            settings=settings,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return _print_run(result)


def _print_run(result: service.RunResult) -> int:
    """Итог service.run в консоль; код выхода: 0 — ok или уже собран, 1 — частично или сбой, 3 — занято."""
    if result.status == "busy":
        print(service.describe_run(result), file=sys.stderr)
        return 3
    if result.status == "failed":
        print(service.describe_run(result), file=sys.stderr)
        return 1
    if result.collect:
        _print_collect(result.collect)
    if result.digest:
        if result.collect:
            print()
        _print_digest(result.digest)
    return 0 if result.status in ("ok", "exists") else 1


def _print_collect(summary: service.CollectSummary) -> None:
    print(f"{'источник':<10} {'статус':<13} {'получено':>8} {'новых':>6} {'догруз.':>7}  ошибка")
    for r in summary.sources:
        note = r.error or (f"предупреждений: {r.warnings}, см. лог" if r.warnings else "")
        print(f"{r.code:<10} {r.status:<13} {r.fetched:>8} {r.new:>6} {r.backfilled:>7}  {note}")
    failed = ", ".join(f"{k}: {v}" for k, v in sorted(summary.texts_failed_by_source.items()))
    print(
        f"\nПолный текст: скачано {summary.texts_fetched}, не удалось {summary.texts_failed}"
        f"{f' ({failed})' if failed else ''}, пропущено {summary.texts_skipped}"
    )
    if summary.rates_error:
        print(f"Курсы ЦБ: ошибка {summary.rates_error}", file=sys.stderr)
    elif summary.rates_date:
        print(f"Курсы ЦБ: на {summary.rates_date}")


def _sources_check(settings: Settings) -> int:
    conn = repo.connect(settings.db_path)
    repo.sync_sources(conn, load_sources())
    row_format = "{:<10} {:<3} {:<10} {:<4} {:<26} {:>4} {:>8} {:>6}  {}"
    print(
        row_format.format(
            "источник", "ур.", "тип", "вкл", "последний успех", "24ч", "без текста", "всего", "ошибка"
        )
    )
    for row in repo.source_report(conn):
        no_text = f"{row['no_text_24h'] / row['items_24h']:.0%}" if row["items_24h"] else "—"
        print(
            row_format.format(
                row["code"],
                row["tier"],
                row["kind"],
                "да" if row["enabled"] else "нет",
                row["last_ok_at"] or "—",
                row["items_24h"],
                no_text,
                row["items_total"],
                row["last_error"] or "",
            )
        )
    counts = repo.reference_counts(conn)
    print(f"\nСправочник актов: {counts['legal_acts']}. Курсы ЦБ: {counts['fx_date'] or 'нет'}")
    return 0


# В CSV для разметки попадают и пары чуть ниже серой зоны: на них видно, не занижен ли порог
REPORT_PAIRS_FROM = 0.15


def _dedup_report(settings: Settings, days: float) -> int:
    conn = repo.connect(settings.db_path)
    sources = load_sources()
    repo.sync_sources(conn, sources)
    until = utc_now()
    window = prepare_window(
        conn,
        sources,
        Keywords.load(),
        settings.dedup,
        until - timedelta(days=days),
        until,
        keep_pairs_from=REPORT_PAIRS_FROM,
    )
    data_dir = settings.db_path.parent
    pairs = write_pairs(data_dir / "dedup_pairs.csv", window)
    write_clusters(data_dir / "dedup_clusters.md", window)
    clusters = window.result.clusters
    print(
        f"Окно: {days:g} сут. Материалов: {len(window.candidates) + len(window.filtered)}, "
        f"отсеяно префильтром: {len(window.filtered)}."
    )
    print(
        f"Сюжетов: {len(clusters)}, с дублями: {sum(len(c.item_ids) > 1 for c in clusters)}, "
        f"правовых: {sum(c.kind == 'legal' for c in clusters)}, пар в серой зоне: {len(window.result.gray)}."
    )
    print(f"Пары для разметки ({pairs}): {data_dir / 'dedup_pairs.csv'}")
    print(f"Кластеры: {data_dir / 'dedup_clusters.md'}")
    return 0


def _verify_laws(settings: Settings, days: float, doc_keys: list[str]) -> int:
    for key in doc_keys:
        try:
            split_doc_key(key)
        except ValueError:
            print(
                f"Неверный ключ акта: {key}. Примеры: UP-236-2026, PKM-594-2026, MJ-1010-2", file=sys.stderr
            )
            return 2
    conn = repo.connect(settings.db_path)
    until = utc_now()
    since = until - timedelta(days=days)
    if not doc_keys:
        # Упоминания актов пишет этап 3: пересчитываем окно, чтобы учесть новые материалы
        sources = load_sources()
        repo.sync_sources(conn, sources)
        prepare_window(conn, sources, Keywords.load(), settings.dedup, since, until)
    started = time.monotonic()
    with HttpClient(settings.http) as http:
        verifier = LexVerifier(conn, http, settings.lex)
        stats = verifier.verify(repo.refs_to_verify(conn, since, until, doc_keys))
        for key in doc_keys:
            if key not in stats.by_key:  # в материалах не упоминается — просто ищем
                with conn:
                    found = verifier.resolve(key)
                stats.by_key[key] = Counter({"есть на lex.uz" if found else "нет на lex.uz": 1})
    elapsed = time.monotonic() - started

    row_format = "{:<22} {:<24} {:<10} {:<9} {}"
    print(row_format.format("акт", "упоминания", "в силе с", "версии", "ссылка"))
    for key, counter in sorted(stats.by_key.items()):
        act = repo.legal_act(conn, key)
        link = act_link(act, "ru") if act else None
        print(
            row_format.format(
                key,
                ", ".join(f"{status} {n}" for status, n in counter.most_common()),
                _ru_date(act["effective_on"]) if act and act["effective_on"] else "—",
                _versions(act) if act else "—",
                link[0] if link else "",
            )
        )
    totals = ", ".join(f"{status} {n}" for status, n in stats.statuses.most_common())
    print(
        f"\nАктов: {len(stats.by_key)}. Упоминания: {totals or 'нет'}. "
        f"Запросов к lex.uz: {stats.requests}, из кэша: {stats.cached}, {elapsed:.0f} с."
    )
    if stats.became_verified:
        print(f"Появились на lex.uz (были pending): {', '.join(stats.became_verified)}")
    for error in stats.errors:
        print(f"Ошибка: {error}", file=sys.stderr)
    return 1 if stats.errors else 0


def _versions(act: sqlite3.Row) -> str:
    """Языки, на которых акт есть на lex.uz: «ru uz en»."""
    present = {"ru": act["lex_id"], "uz": act["uz_latn_lex_id"] or act["uz_lex_id"], "en": act["en_lex_id"]}
    return " ".join(lang for lang, lex_id in present.items() if lex_id) or "—"


def _ru_date(iso_date: str) -> str:
    return datetime.fromisoformat(iso_date).strftime("%d.%m.%Y")


def _draft(settings: Settings, days: float, *, use_llm: bool) -> int:
    conn = repo.connect(settings.db_path)
    sources = load_sources()
    repo.sync_sources(conn, sources)
    until = utc_now()
    since = until - timedelta(days=days)
    run_id = repo.start_run(conn, "digest")
    started = time.monotonic()
    window = prepare_window(conn, sources, Keywords.load(), settings.dedup, since, until)
    with HttpClient(settings.http) as http:
        lex = verify_window(conn, http, settings.lex, since, until)
    llm_settings = settings.llm if use_llm else settings.llm.model_copy(update={"max_calls_per_run": 0})
    budget = LlmBudget(llm_settings)
    outcome = process_window(conn, settings, window, budget, until)
    elapsed = time.monotonic() - started

    articles = outcome.articles
    no_llm = sum(not a.by_llm for a in articles)
    no_translation = sum(a.texts[lang] is None for a in articles for lang in ("en", "uz"))
    flagged = sum(bool(a.flags) for a in articles)
    relevant = sum(v.relevant for v in outcome.verdicts.values())
    stats = {
        "window": {"from": to_iso(since), "to": to_iso(until)},
        "items": len(window.candidates),
        "relevant": relevant,
        "clusters": len(outcome.result.clusters),
        "articles": len(articles),
        "legal": sum(a.doc_key is not None for a in articles),
        "no_llm": no_llm,
        "no_translation": no_translation,
        "qa_flagged": flagged,
        "lex": {"requests": lex.requests, "errors": len(lex.errors)},
        "llm": budget.stats(),
        "elapsed_s": round(elapsed),
    }
    date = tashkent_date(until).isoformat()
    path = write_draft(
        settings.db_path.parent / "draft" / f"{date}.md",
        outcome,
        {
            "date": date,
            "окно": f"{to_iso(since)} — {to_iso(until)}",
            "материалов после префильтра": len(window.candidates),
            "подходят по оценке Claude": relevant,
            "сюжетов": len(outcome.result.clusters),
            "в черновике": f"{len(articles)} (правовых {stats['legal']})",
            "Claude": _llm_summary(budget.stats()),
        },
    )
    repo.finish_run(conn, run_id, "draft", stats)

    print(
        f"Окно {days:g} сут.: материалов {len(window.candidates)}, подходят {relevant}, "
        f"сюжетов {len(outcome.result.clusters)}, в черновике {len(articles)} (правовых {stats['legal']})."
    )
    print(f"Без ИИ-обработки: {no_llm}, без перевода: {no_translation}, с замечаниями проверки: {flagged}.")
    print(f"Claude: {_llm_summary(budget.stats())}. Всего {elapsed:.0f} с.")
    print(f"Черновик: {path}")
    return 1 if budget.stopped and use_llm else 0


def _digest(settings: Settings, *, force: bool, use_llm: bool) -> int:
    return _print_run(service.run("digest", force=force, use_llm=use_llm, settings=settings))


def _print_digest(digest: service.DigestSummary) -> None:
    if digest.status == "exists":
        print(f"Дайджест за {digest.date.isoformat()} уже собран: {digest.dir}")
        print("Пересобрать с новостями, вышедшими после него: uzdigest digest --force")
        return
    stats = digest.stats
    hours = (digest.window_to - digest.window_from).total_seconds() / 3600
    trimmed = " (обрезано по max_window_h)" if digest.trimmed else ""
    print(
        f"Окно: {_local(digest.window_from)} — {_local(digest.window_to)} по Ташкенту, "
        f"{hours:.0f} ч{trimmed}."
    )
    print(
        f"Материалов {stats['items']}, подходят {stats['relevant']}, сюжетов {stats['clusters']}. "
        f"В дайджесте {digest.articles} (правовых {digest.legal}), обновлений {digest.updates}."
    )
    print(
        f"Без ИИ-обработки: {digest.no_llm}, без перевода: {digest.no_translation}, "
        f"с замечаниями проверки: {digest.qa_flagged}."
    )
    if digest.failed_sources:
        print("Не ответили: " + ", ".join(f"{code} ({error})" for code, error in digest.failed_sources))
    print(
        f"Claude: {_llm_summary(stats['llm'])}. lex.uz: запросов {stats['lex']['requests']}, "
        f"ошибок {stats['lex']['errors']}. Всего {stats['elapsed_s']} с."
    )
    print(f"Дайджест: {digest.dir}")


def _rerender(settings: Settings, day: date) -> int:
    conn = repo.connect(settings.db_path)
    try:
        out_dir = rerender(conn, settings, day)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Файлы пересобраны из БД: {out_dir}")
    return 0


def _topics(settings: Settings, day: date | None) -> int:
    try:
        digest_date, topics = service.topics(day, settings=settings)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    categories = service.categories_ru()
    print(f"Темы дайджеста за {digest_date.isoformat()}:")
    current = None
    for topic in topics:
        if topic.category != current:
            current = topic.category
            print(f"\n{categories.get(current, current)}")
        doc = f" [{topic.doc_key}]" if topic.doc_key else ""
        print(f"  {topic.number} · важность {topic.importance}{doc} · {topic.headline}")
    print("\nПост: uzdigest post НОМЕР --platform telegram|linkedin|threads|instagram [--lang ru|en|uz]")
    return 0


def _post(settings: Settings, args: argparse.Namespace) -> int:
    try:
        post = service.post(
            args.article,
            args.platform,
            args.lang,
            args.mode,
            day=args.date,
            author=args.author,
            settings=settings,
        )
    except (LookupError, ValueError, PostError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if not isinstance(exc, PostError) else 1
    print(post.text)
    print("\n" + "─" * 40)
    source = "из кэша, Claude не вызывался" if post.cached else f"вызовов Claude: {post.calls}"
    length = f"{post.chars}/{post.max_chars} символов"
    print(f"{post.article_id} · {post.platform} · {post.lang} · {post.mode} · {length} · {source}")
    if post.flags:
        print(f"⚠️ Проверить: {'; '.join(post.flags)}")
    if post.notes:
        print(f"Claude: {post.notes}")
    print(f"Файл: {post.path}")
    return 1 if post.flags else 0


def _status(settings: Settings, as_json: bool) -> int:
    state = service.status(settings=settings)
    print(state.model_dump_json(indent=2) if as_json else service.describe_status(state))
    return 0


def _metrics(settings: Settings, days: int, as_json: bool) -> int:
    state = service.metrics(days, settings=settings)
    print(state.model_dump_json(indent=2) if as_json else render_text(state))
    return 0


def _feedback(settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "add":
        try:
            item = service.add_feedback(
                args.kind,
                args.text,
                day=args.date,
                article_id=args.article,
                author=args.author,
                settings=settings,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"Замечание {item.id} записано: {service.FEEDBACK_KINDS[item.kind]}, "
            f"{item.article_id or 'весь день'}"
        )
        return 0
    if args.action == "export":
        path = service.export_feedback(Path(args.path), args.date, settings=settings)
        print(f"Замечания выгружены: {path}")
        return 0
    items = service.list_feedback(args.date, settings=settings)
    for f in items:
        where = f.article_id or (f.digest_date.isoformat() if f.digest_date else "—")
        kind = service.FEEDBACK_KINDS[f.kind]
        print(f"{f.id:>4} · {_local(f.created_at)} · {kind} · {where} · {f.author or '—'} · {f.text}")
    print(f"Всего: {len(items)}")
    return 0


def _bot(settings: Settings) -> int:
    from uzdigest.bot.app import BotTokenMissing, run_bot
    from uzdigest.ops import Busy

    try:
        run_bot(settings)
    except BotTokenMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Busy:
        print("Бот уже запущен (data/bot.lock занят другим процессом)", file=sys.stderr)
        return 3
    return 0


def _local(moment: datetime) -> str:
    return moment.astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M")


def _llm_summary(llm: dict) -> str:
    """Расход Claude из LlmBudget.stats()."""
    usage = llm.get("usage") or {}
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    tokens_in = sum(usage.get(key, 0) for key in keys)
    summary = (
        f"вызовов {llm['calls']} (неудачных {llm['failed']}), токенов {tokens_in} на вход "
        f"(из кэша {usage.get('cache_read_input_tokens', 0)}), {usage.get('output_tokens', 0)} на выход, "
        f"{llm['duration_ms'] / 1000:.0f} с"
    )
    if llm.get("stopped"):
        summary += f"; резервный режим: {llm['stopped']}"
    return summary


class _Ping(BaseModel):
    capital: str
    currency_code: str


def _llm_check(settings: Settings) -> int:
    try:
        result = ask_json(
            "Назови столицу Узбекистана и трёхбуквенный код его валюты по ISO 4217.",
            _Ping,
            settings=settings.llm,
        )
    except LlmError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Модель: {settings.llm.model}, effort: {settings.llm.effort}, {result.duration_ms} мс")
    print(f"Ответ: {result.data.model_dump_json()}")
    print(f"Лимит вызовов за запуск: {settings.llm.max_calls_per_run}")
    limit = result.rate_limit
    if limit:
        utilization = (
            f", израсходовано {limit['utilization']:.0%}" if limit.get("utilization") is not None else ""
        )
        print(f"Лимит подписки: {limit['status']} ({limit.get('type') or 'тип не указан'}{utilization})")
    else:
        print("Лимит подписки: SDK не прислал состояние (значит, лимит далеко)")
    return 0
