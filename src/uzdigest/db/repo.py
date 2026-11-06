"""Доступ к SQLite: подключение, миграции и запросы конвейера. В БД пишет только конвейер."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from uzdigest.config import Source
from uzdigest.models import FxRate, Item, LegalAct
from uzdigest.redact import mask
from uzdigest.timeutil import from_iso, to_iso, utc_now

log = logging.getLogger(__name__)

BUSY_TIMEOUT_S = 30  # бот пишет посты, пока идёт сборка: ждём дольше, чем отвечает медленный lex.uz
# Материал окна: вышел в окне, либо вышел раньше (не глубже late_from), но собран уже в окне: источник
# отставал или сборка опередила сбор. Иначе такой материал не попал бы ни в один дайджест
_IN_WINDOW = (
    "(i.published_at >= :since OR (i.fetched_at >= :since AND i.published_at >= :late_from))"
    " AND i.published_at < :until"
)


def _window_params(since: datetime, until: datetime, late_from: datetime | None) -> dict[str, str]:
    return {"since": to_iso(since), "until": to_iso(until), "late_from": to_iso(late_from or since)}


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Открывает БД и применяет недостающие миграции. ':memory:' — для тестов."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Применяет файлы db/schema/NNN_*.sql, которых ещё нет в schema_version, каждый в своей транзакции."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    scripts = sorted(
        (f for f in (resources.files("uzdigest.db") / "schema").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    for script in scripts:
        version = int(script.name.split("_", 1)[0])
        if version in applied:
            continue
        sql = script.read_text(encoding="utf-8")
        conn.executescript(
            f"BEGIN;\n{sql}\n"
            f"INSERT INTO schema_version (version, applied_at) VALUES ({version}, '{to_iso(utc_now())}');\n"
            "COMMIT;"
        )


def sync_sources(conn: sqlite3.Connection, sources: list[Source]) -> dict[str, int]:
    """Приводит таблицу sources к реестру из sources.yaml. Источники, убранные из реестра, выключаются."""
    with conn:
        for s in sources:
            conn.execute(
                """
                INSERT INTO sources (code, name, kind, url, tier, lang, params_json, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (code) DO UPDATE SET
                  name = excluded.name, kind = excluded.kind, url = excluded.url, tier = excluded.tier,
                  lang = excluded.lang, params_json = excluded.params_json, enabled = excluded.enabled
                """,
                (s.code, s.name, s.kind, s.url, s.tier, s.lang, json.dumps(s.params), int(s.enabled)),
            )
        codes = [s.code for s in sources]
        conn.execute(
            f"UPDATE sources SET enabled = 0 WHERE code NOT IN ({','.join('?' * len(codes))})", codes
        )
    return {row["code"]: row["id"] for row in conn.execute("SELECT id, code FROM sources")}


def source_state(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    """etag, last_modified и last_ok_at источника."""
    return conn.execute(
        "SELECT etag, last_modified, last_ok_at FROM sources WHERE id = ?", (source_id,)
    ).fetchone()


def known_urls(conn: sqlite3.Connection, source_id: int, since: datetime) -> frozenset[str]:
    """Канонические адреса материалов источника, собранных не раньше since."""
    rows = conn.execute(
        "SELECT canonical_url FROM items WHERE source_id = ? AND fetched_at >= ?", (source_id, to_iso(since))
    )
    return frozenset(row[0] for row in rows)


def mark_source_ok(
    conn: sqlite3.Connection, source_id: int, etag: str | None, last_modified: str | None
) -> None:
    conn.execute(
        "UPDATE sources SET last_ok_at = ?, last_error = NULL, etag = ?, last_modified = ? WHERE id = ?",
        (to_iso(utc_now()), etag, last_modified, source_id),
    )


def mark_source_error(conn: sqlite3.Connection, source_id: int, error: str) -> None:
    with conn:
        conn.execute("UPDATE sources SET last_error = ? WHERE id = ?", (mask(error), source_id))


def start_run(conn: sqlite3.Connection, kind: str) -> int:
    with conn:
        cur = conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, to_iso(utc_now())))
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    stats: dict[str, Any],
    window: tuple[datetime, datetime] | None = None,
) -> None:
    window_from, window_to = (to_iso(window[0]), to_iso(window[1])) if window else (None, None)
    with conn:
        conn.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, stats_json = ?,
                            window_from = COALESCE(?, window_from), window_to = COALESCE(?, window_to)
            WHERE id = ?
            """,
            (
                to_iso(utc_now()),
                status,
                mask(json.dumps(stats, ensure_ascii=False)),
                window_from,
                window_to,
                run_id,
            ),
        )


def insert_item(conn: sqlite3.Connection, source_id: int, run_id: int, item: Item) -> bool:
    """Записывает материал. Возвращает False, если он уже есть (тот же канонический URL или GUID)."""
    categories = json.dumps(list(item.categories), ensure_ascii=False) if item.categories else None
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO items (source_id, guid, url, canonical_url, title, title_norm, lead, body,
                                     lang, published_at, published_precision, fetched_at, run_id,
                                     text_status, categories)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            item.guid,
            item.url,
            item.canonical_url,
            item.title,
            item.title_norm,
            item.lead,
            item.body,
            item.lang,
            to_iso(item.published_at),
            item.published_precision,
            to_iso(item.fetched_at),
            run_id,
            item.text_status,
            categories,
        ),
    )
    if cur.rowcount == 1:
        return True
    if categories:
        # Материал записан до того, как стали хранить рубрики: дополняем, раз лента их отдала
        conn.execute(
            "UPDATE items SET categories = ? WHERE canonical_url = ? AND categories IS NULL",
            (categories, item.canonical_url),
        )
    return False


def upsert_legal_act(conn: sqlite3.Connection, act: LegalAct) -> bool:
    """Записывает акт в справочник или дополняет известный. Возвращает True, если акт новый."""
    exists = conn.execute("SELECT 1 FROM legal_acts WHERE doc_key = ?", (act.doc_key,)).fetchone()
    try:
        _upsert_legal_act(conn, act)
    except sqlite3.IntegrityError as exc:
        # Тот же lex_id уже записан под другим ключом: справочник важнее одной записи ленты
        log.warning("Акт %s (lex_id=%s) не записан: %s", act.doc_key, act.lex_id, exc)
        return False
    return exists is None


def _upsert_legal_act(conn: sqlite3.Connection, act: LegalAct) -> None:
    conn.execute(
        """
        INSERT INTO legal_acts (doc_key, doc_type, number, issuer, title, adopted_on, effective_on,
                                lex_id, lex_url, source, uz_lex_id, uz_latn_lex_id, en_lex_id, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (doc_key) DO UPDATE SET
          issuer = COALESCE(excluded.issuer, issuer),
          title = COALESCE(excluded.title, title),
          adopted_on = COALESCE(excluded.adopted_on, adopted_on),
          effective_on = COALESCE(excluded.effective_on, effective_on),
          lex_id = COALESCE(lex_id, excluded.lex_id),
          lex_url = COALESCE(lex_url, excluded.lex_url),
          uz_lex_id = COALESCE(excluded.uz_lex_id, uz_lex_id),
          uz_latn_lex_id = COALESCE(excluded.uz_latn_lex_id, uz_latn_lex_id),
          en_lex_id = COALESCE(excluded.en_lex_id, en_lex_id),
          fetched_at = excluded.fetched_at
        """,
        (
            act.doc_key,
            act.doc_type,
            act.number,
            act.issuer,
            act.title,
            act.adopted_on.isoformat() if act.adopted_on else None,
            act.effective_on.isoformat() if act.effective_on else None,
            act.lex_id,
            act.lex_url,
            act.source,
            act.uz_lex_id,
            act.uz_latn_lex_id,
            act.en_lex_id,
            to_iso(utc_now()),
        ),
    )


def upsert_fx_rates(conn: sqlite3.Connection, rates: list[FxRate]) -> None:
    fetched_at = to_iso(utc_now())
    with conn:
        conn.executemany(
            """
            INSERT INTO fx_rates (date, ccy, nominal, rate, diff, fetched_at) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (date, ccy) DO UPDATE SET
              nominal = excluded.nominal, rate = excluded.rate, diff = excluded.diff,
              fetched_at = excluded.fetched_at
            """,
            [(r.date.isoformat(), r.ccy, r.nominal, r.rate, r.diff, fetched_at) for r in rates],
        )


def items_to_enrich(
    conn: sqlite3.Connection, since: datetime, max_attempts: int, limit: int
) -> list[sqlite3.Row]:
    """Материалы без текста: ещё не обработанные и неудачные, у которых остались попытки.

    Сначала источники уровня A, затем B и C; внутри уровня — свежие первыми.
    """
    return conn.execute(
        """
        SELECT i.id, i.url, i.title, i.published_at, i.published_precision, s.code AS source
        FROM items i JOIN sources s ON s.id = i.source_id
        WHERE (i.text_status IS NULL OR (i.text_status = 'failed' AND i.text_attempts < ?))
          AND i.published_at >= ?
        ORDER BY s.tier, i.published_at DESC
        LIMIT ?
        """,
        (max_attempts, to_iso(since), limit),
    ).fetchall()


def save_text(
    conn: sqlite3.Connection, item_id: int, title: str, title_norm: str, body: str, published_at: datetime
) -> None:
    conn.execute(
        """
        UPDATE items SET title = ?, title_norm = ?, body = ?, published_at = ?,
                         text_status = 'fetched', text_error = NULL, text_attempts = text_attempts + 1
        WHERE id = ?
        """,
        (title, title_norm, body, to_iso(published_at), item_id),
    )


def mark_text_failed(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE items SET text_status = 'failed', text_error = ?, text_attempts = text_attempts + 1
        WHERE id = ?
        """,
        (error, item_id),
    )


def mark_text_skipped(conn: sqlite3.Connection, item_ids: list[int]) -> None:
    with conn:
        conn.executemany("UPDATE items SET text_status = 'skipped' WHERE id = ?", [(i,) for i in item_ids])


def source_report(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Состояние источников для команды sources-check."""
    since = to_iso(utc_now() - timedelta(hours=24))
    return conn.execute(
        """
        SELECT s.code, s.tier, s.kind, s.enabled, s.last_ok_at, s.last_error,
               COUNT(i.id) AS items_total,
               COALESCE(SUM(i.published_at >= ?), 0) AS items_24h,
               COALESCE(SUM(i.published_at >= ? AND i.text_status = 'failed'), 0) AS no_text_24h
        FROM sources s LEFT JOIN items i ON i.source_id = s.id
        GROUP BY s.id
        ORDER BY s.tier, s.code
        """,
        (since, since),
    ).fetchall()


def reference_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Размер справочника актов и дата последних курсов ЦБ — для sources-check."""
    return {
        "legal_acts": conn.execute("SELECT COUNT(*) FROM legal_acts").fetchone()[0],
        "fx_date": conn.execute("SELECT MAX(date) FROM fx_rates").fetchone()[0],
    }


# --- Этап 3: окно дайджеста, префильтр, правовые ключи, кластеры ---


def window_items(
    conn: sqlite3.Connection, since: datetime, until: datetime, late_from: datetime | None = None
) -> list[sqlite3.Row]:
    """Материалы с заголовком из окна [since, until). Без late_from — только вышедшие в окне."""
    return conn.execute(
        f"""
        SELECT i.id, s.code AS source, s.tier, i.title, COALESCE(i.body, i.lead) AS text, i.url,
               i.published_at, i.categories
        FROM items i JOIN sources s ON s.id = i.source_id
        WHERE {_IN_WINDOW} AND i.title != ''
        ORDER BY i.published_at, i.id
        """,
        _window_params(since, until, late_from),
    ).fetchall()


def save_prefilter(conn: sqlite3.Connection, item_id: int, passed: bool, reason: str) -> None:
    conn.execute(
        "UPDATE items SET prefilter = ?, prefilter_reason = ? WHERE id = ?",
        ("pass" if passed else "filtered", reason, item_id),
    )


def save_legal_refs(conn: sqlite3.Connection, item_id: int, refs: list[Any]) -> None:
    """Упоминания актов (legal.doc_key.LegalRef) взамен прежних.

    Ключи, которых в материале больше нет (извлечение стало точнее), удаляются. У оставшихся статус
    сверки и время проверки не трогаются.
    """
    keys = [r.doc_key for r in refs]
    conn.execute(
        f"DELETE FROM item_legal_refs WHERE item_id = ? AND doc_key NOT IN ({','.join('?' * len(keys))})",
        [item_id, *keys],
    )
    conn.executemany(
        """
        INSERT INTO item_legal_refs (item_id, doc_key, raw_mention, is_draft, year_guessed)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (item_id, doc_key) DO UPDATE SET
          raw_mention = excluded.raw_mention, is_draft = excluded.is_draft,
          year_guessed = excluded.year_guessed
        """,
        [(item_id, r.doc_key, r.raw_mention, int(r.is_draft), int(r.year_guessed)) for r in refs],
    )


DECISION_PRIORITY = {"rule": 0, "llm": 1, "manual": 2}


def pair_decisions(conn: sqlite3.Connection, item_ids: list[int]) -> dict[tuple[int, int], str]:
    """Решения по парам среди item_ids; при нескольких источниках решения побеждает manual > llm > rule."""
    if not item_ids:
        return {}
    ids = set(item_ids)
    rows = conn.execute(
        f"SELECT item_a, item_b, decision, source FROM pair_decisions "
        f"WHERE item_a IN ({','.join('?' * len(ids))})",
        list(ids),
    ).fetchall()
    best: dict[tuple[int, int], tuple[int, str]] = {}
    for row in rows:
        if row["item_b"] not in ids:
            continue
        key = (row["item_a"], row["item_b"])
        priority = DECISION_PRIORITY[row["source"]]
        if key not in best or priority > best[key][0]:
            best[key] = (priority, row["decision"])
    return {key: decision for key, (_, decision) in best.items()}


def save_pair_decision(conn: sqlite3.Connection, a: int, b: int, decision: str, source: str) -> None:
    a, b = sorted((a, b))
    conn.execute(
        """
        INSERT INTO pair_decisions (item_a, item_b, decision, source, created_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (item_a, item_b, source) DO UPDATE SET
          decision = excluded.decision, created_at = excluded.created_at
        """,
        (a, b, decision, source, to_iso(utc_now())),
    )


def item_cluster_ids(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, int]:
    if not item_ids:
        return {}
    rows = conn.execute(
        f"SELECT id, cluster_id FROM items WHERE cluster_id IS NOT NULL "
        f"AND id IN ({','.join('?' * len(item_ids))})",
        item_ids,
    )
    return {row["id"]: row["cluster_id"] for row in rows}


def upsert_cluster(
    conn: sqlite3.Connection,
    cluster_id: int | None,
    kind: str,
    primary_item_id: int,
    doc_key: str | None,
    first_seen_at: str,
) -> int:
    if cluster_id is None:
        cur = conn.execute(
            "INSERT INTO clusters (kind, primary_item_id, doc_key, first_seen_at) VALUES (?, ?, ?, ?)",
            (kind, primary_item_id, doc_key, first_seen_at),
        )
        return cur.lastrowid
    conn.execute(
        "UPDATE clusters SET kind = ?, primary_item_id = ?, doc_key = ? WHERE id = ?",
        (kind, primary_item_id, doc_key, cluster_id),
    )
    return cluster_id


# Статусы, которые даёт кластеризация; summarized и published ставят следующие этапы, их не трогаем
_CLUSTER_STATUSES = "('new', 'filtered', 'duplicate', 'primary')"


def assign_cluster(conn: sqlite3.Connection, cluster_id: int, primary_id: int, item_ids: list[int]) -> None:
    conn.executemany(
        f"""
        UPDATE items SET cluster_id = ?,
          status = CASE WHEN status IN {_CLUSTER_STATUSES} THEN ? ELSE status END
        WHERE id = ?
        """,
        [(cluster_id, "primary" if i == primary_id else "duplicate", i) for i in item_ids],
    )


def mark_filtered(conn: sqlite3.Connection, item_ids: list[int]) -> None:
    conn.executemany(
        f"UPDATE items SET status = 'filtered', cluster_id = NULL"
        f" WHERE id = ? AND status IN {_CLUSTER_STATUSES}",
        [(i,) for i in item_ids],
    )


def published_clusters(conn: sqlite3.Connection, since_date: str, before_date: str) -> list[sqlite3.Row]:
    """Кластеры из дайджестов за [since_date, before_date) (ГГГГ-ММ-ДД) с текстом главного материала.

    Дайджест за сам день сборки не учитывается: пересборка дня не должна считать свои сюжеты повторами.
    """
    return conn.execute(
        """
        SELECT DISTINCT c.id, c.doc_key, c.primary_item_id, i.title, COALESCE(i.body, i.lead) AS text
        FROM digest_entries de
        JOIN digests d ON d.id = de.digest_id
        JOIN clusters c ON c.id = de.cluster_id
        JOIN items i ON i.id = c.primary_item_id
        WHERE d.date >= ? AND d.date < ?
        """,
        (since_date, before_date),
    ).fetchall()


# --- Этап 4: сверка с lex.uz ---


def legal_act(conn: sqlite3.Connection, doc_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM legal_acts WHERE doc_key = ?", (doc_key,)).fetchone()


def refs_to_verify(
    conn: sqlite3.Connection,
    since: datetime,
    until: datetime,
    doc_keys: list[str] | None = None,
    late_from: datetime | None = None,
) -> list[sqlite3.Row]:
    """Упоминания актов для сверки, по doc_key.

    Без doc_keys — в материалах окна, прошедших префильтр, и все ещё не найденные (pending)
    из прошлых окон. С doc_keys — все упоминания этих актов, без окна.
    """
    params: dict[str, str] | list[str]
    if doc_keys:
        where, params = f"r.doc_key IN ({','.join('?' * len(doc_keys))})", list(doc_keys)
    else:
        where = f"({_IN_WINDOW} AND i.prefilter = 'pass') OR r.verify_status = 'pending'"
        params = _window_params(since, until, late_from)
    return conn.execute(
        f"""
        SELECT r.item_id, r.doc_key, r.is_draft, r.year_guessed, r.verify_status, i.published_at
        FROM item_legal_refs r JOIN items i ON i.id = r.item_id
        WHERE {where}
        ORDER BY r.doc_key, i.published_at
        """,
        params,
    ).fetchall()


def save_verify_status(conn: sqlite3.Connection, item_id: int, doc_key: str, status: str) -> None:
    conn.execute(
        "UPDATE item_legal_refs SET verify_status = ?, checked_at = ? WHERE item_id = ? AND doc_key = ?",
        (status, to_iso(utc_now()), item_id, doc_key),
    )


def lex_lookup(conn: sqlite3.Connection, query: str, fresh_since: datetime) -> str | None:
    """Кэшированный ответ lex.uz на запрос, если он получен не раньше fresh_since."""
    row = conn.execute(
        "SELECT result_json FROM lex_lookups WHERE query = ? AND fetched_at >= ?",
        (query, to_iso(fresh_since)),
    ).fetchone()
    return row[0] if row else None


def save_lex_lookup(conn: sqlite3.Connection, query: str, result_json: str, fetched_at: datetime) -> None:
    conn.execute(
        """
        INSERT INTO lex_lookups (query, fetched_at, result_json) VALUES (?, ?, ?)
        ON CONFLICT (query) DO UPDATE SET fetched_at = excluded.fetched_at, result_json = excluded.result_json
        """,
        (query, to_iso(fetched_at), result_json),
    )


# --- Этап 5: Claude ---


def triage_rows(conn: sqlite3.Connection, item_ids: list[int], prompt_version: str) -> dict[int, sqlite3.Row]:
    if not item_ids:
        return {}
    rows = conn.execute(
        f"SELECT * FROM triage WHERE prompt_version = ? AND item_id IN ({','.join('?' * len(item_ids))})",
        [prompt_version, *item_ids],
    )
    return {row["item_id"]: row for row in rows}


def save_triage(
    conn: sqlite3.Connection,
    item_id: int,
    prompt_version: str,
    relevant: bool,
    category: str | None,
    importance: int,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO triage (item_id, prompt_version, relevant, category, importance, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (item_id, prompt_version) DO UPDATE SET
          relevant = excluded.relevant, category = excluded.category, importance = excluded.importance,
          reason = excluded.reason, created_at = excluded.created_at
        """,
        (item_id, prompt_version, int(relevant), category, importance, reason, to_iso(utc_now())),
    )


def decided_pairs(conn: sqlite3.Connection, item_ids: list[int]) -> set[tuple[int, int]]:
    """Пары, по которым уже решили Claude или команда: Claude их больше не спрашиваем."""
    if not item_ids:
        return set()
    rows = conn.execute(
        f"SELECT item_a, item_b FROM pair_decisions WHERE source IN ('llm', 'manual') "
        f"AND item_a IN ({','.join('?' * len(item_ids))})",
        item_ids,
    )
    return {(row["item_a"], row["item_b"]) for row in rows}


def doc_statuses(conn: sqlite3.Connection, item_ids: list[int]) -> dict[str, set[str]]:
    """Статусы сверки упоминаний актов в этих материалах: doc_key → статусы."""
    if not item_ids:
        return {}
    rows = conn.execute(
        f"SELECT doc_key, verify_status FROM item_legal_refs WHERE verify_status IS NOT NULL "
        f"AND item_id IN ({','.join('?' * len(item_ids))})",
        item_ids,
    )
    statuses: dict[str, set[str]] = {}
    for row in rows:
        statuses.setdefault(row["doc_key"], set()).add(row["verify_status"])
    return statuses


def summary_row(
    conn: sqlite3.Connection, cluster_id: int, prompt_version: str, lang: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM summaries WHERE cluster_id = ? AND prompt_version = ? AND lang = ?",
        (cluster_id, prompt_version, lang),
    ).fetchone()


def save_summary(
    conn: sqlite3.Connection,
    *,
    cluster_id: int,
    prompt_version: str,
    lang: str,
    model: str,
    category: str | None,
    importance: int | None,
    headline: str,
    summary: str,
    details: str,
    qa_flags: list[str],
    input_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO summaries (cluster_id, prompt_version, lang, model, category, importance, headline,
                               summary, details, qa_flags, input_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (cluster_id, prompt_version, lang) DO UPDATE SET
          model = excluded.model, category = excluded.category, importance = excluded.importance,
          headline = excluded.headline, summary = excluded.summary, details = excluded.details,
          qa_flags = excluded.qa_flags, input_hash = excluded.input_hash, created_at = excluded.created_at
        """,
        (
            cluster_id,
            prompt_version,
            lang,
            model,
            category,
            importance,
            headline,
            summary,
            details,
            json.dumps(qa_flags, ensure_ascii=False),
            input_hash,
            to_iso(utc_now()),
        ),
    )


# --- Этап 6: сборка дайджеста ---


def last_digest_end(conn: sqlite3.Connection) -> datetime | None:
    """Конец окна последнего дайджеста, закрывшего своё окно: начало окна следующего.

    Дайджест без Claude или без статей окно не закрывает (stats_json: complete = false): новости,
    которые в него не вошли, войдут в следующий, а то, что вышло, отсеется как повтор.
    """
    row = conn.execute(
        """
        SELECT MAX(window_to) FROM runs
        WHERE kind = 'digest' AND status IN ('ok', 'partial')
          AND COALESCE(json_extract(stats_json, '$.complete'), 1) = 1
        """
    ).fetchone()
    return from_iso(row[0]) if row[0] else None


def digest_by_date(conn: sqlite3.Connection, day: str) -> sqlite3.Row | None:
    """Дайджест за дату ГГГГ-ММ-ДД с окном запуска, который его собрал."""
    return conn.execute(
        """
        SELECT d.id, d.date, d.dir, d.run_id, d.model_json, r.window_from, r.window_to
        FROM digests d LEFT JOIN runs r ON r.id = d.run_id
        WHERE d.date = ?
        """,
        (day,),
    ).fetchone()


def save_digest(
    conn: sqlite3.Connection, day: str, out_dir: str, run_id: int, model_json: str, cluster_ids: list[int]
) -> int:
    """Записывает дайджест дня и его сюжеты; пересборка дня заменяет прежний список сюжетов."""
    with conn:
        conn.execute(
            """
            INSERT INTO digests (date, dir, run_id, created_at, model_json) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (date) DO UPDATE SET
              dir = excluded.dir, run_id = excluded.run_id, created_at = excluded.created_at,
              model_json = excluded.model_json
            """,
            (day, out_dir, run_id, to_iso(utc_now()), model_json),
        )
        digest_id = conn.execute("SELECT id FROM digests WHERE date = ?", (day,)).fetchone()[0]
        conn.execute("DELETE FROM digest_entries WHERE digest_id = ?", (digest_id,))
        conn.executemany(
            "INSERT INTO digest_entries (digest_id, cluster_id, position) VALUES (?, ?, ?)",
            [(digest_id, cluster_id, n) for n, cluster_id in enumerate(cluster_ids, 1)],
        )
    return digest_id


def digest_models(conn: sqlite3.Connection, since_date: str, before_date: str) -> list[sqlite3.Row]:
    """date и model_json дайджестов за [since_date, before_date), старые первыми."""
    return conn.execute(
        "SELECT date, model_json FROM digests WHERE date >= ? AND date < ? AND model_json IS NOT NULL "
        "ORDER BY date",
        (since_date, before_date),
    ).fetchall()


def key_statuses(conn: sqlite3.Connection, doc_keys: list[str]) -> dict[str, set[str]]:
    """Статусы сверки всех упоминаний этих актов, без окна: doc_key → статусы."""
    if not doc_keys:
        return {}
    rows = conn.execute(
        f"SELECT doc_key, verify_status FROM item_legal_refs WHERE verify_status IS NOT NULL "
        f"AND doc_key IN ({','.join('?' * len(doc_keys))})",
        doc_keys,
    )
    statuses: dict[str, set[str]] = {}
    for row in rows:
        statuses.setdefault(row["doc_key"], set()).add(row["verify_status"])
    return statuses


def fx_rates_on(conn: sqlite3.Connection, day: str, currencies: list[str]) -> list[sqlite3.Row]:
    """Курсы ЦБ на последнюю дату не позже day — только эти валюты, в их порядке."""
    row = conn.execute("SELECT MAX(date) FROM fx_rates WHERE date <= ?", (day,)).fetchone()
    if row[0] is None or not currencies:
        return []
    rows = {
        r["ccy"]: r
        for r in conn.execute(
            f"SELECT date, ccy, nominal, rate, diff FROM fx_rates WHERE date = ? "
            f"AND ccy IN ({','.join('?' * len(currencies))})",
            [row[0], *currencies],
        )
    }
    return [rows[c] for c in currencies if c in rows]


def items_precision(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, str]:
    """Точность даты публикации: time или day (источник знает только дату)."""
    if not item_ids:
        return {}
    rows = conn.execute(
        f"SELECT id, published_precision FROM items WHERE id IN ({','.join('?' * len(item_ids))})",
        item_ids,
    )
    return {row["id"]: row["published_precision"] for row in rows}


def source_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Включённые источники: последний успешный опрос и последняя ошибка — для подвала дайджеста."""
    return conn.execute(
        "SELECT code, name, url, last_ok_at, last_error FROM sources WHERE enabled = 1 ORDER BY tier, code"
    ).fetchall()


def latest_digest_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) FROM digests WHERE model_json IS NOT NULL").fetchone()
    return row[0]


def digest_list(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """date и число статей (articles) всех собранных дайджестов, новые первыми."""
    return conn.execute(
        "SELECT date, json_array_length(model_json, '$.articles') AS articles FROM digests "
        "WHERE model_json IS NOT NULL ORDER BY date DESC"
    ).fetchall()


# --- Этап 7: посты ---


def save_post(conn: sqlite3.Connection, post: dict[str, Any]) -> int:
    """Записывает пост; списки (hashtags, qa_flags) — JSON. Возвращает id."""
    values = {
        **post,
        "hashtags": json.dumps(post["hashtags"], ensure_ascii=False),
        "qa_flags": json.dumps(post["qa_flags"], ensure_ascii=False),
        "created_at": to_iso(post["created_at"]),
    }
    with conn:
        cur = conn.execute(
            f"INSERT INTO posts ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )
    return cur.lastrowid


def posts_for(
    conn: sqlite3.Connection, day: str, article_id: str, platform: str, lang: str
) -> list[sqlite3.Row]:
    """Посты по статье для площадки и языка, старые первыми."""
    return conn.execute(
        "SELECT * FROM posts WHERE digest_date = ? AND article_id = ? AND platform = ? AND lang = ? "
        "ORDER BY id",
        (day, article_id, platform, lang),
    ).fetchall()


def count_posts_since(conn: sqlite3.Connection, since: datetime) -> int:
    """Сколько постов сделано Claude с момента since (взятые из кэша не записываются)."""
    return conn.execute("SELECT COUNT(*) FROM posts WHERE created_at >= ?", (to_iso(since),)).fetchone()[0]


def post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


# --- Этап 8: слой управления ---


def last_run(conn: sqlite3.Connection, kind: str, statuses: tuple[str, ...] = ()) -> sqlite3.Row | None:
    """Последний завершённый запуск этого вида (по желанию — только с этими статусами)."""
    where, params = "kind = ? AND finished_at IS NOT NULL", [kind]
    if statuses:
        where += f" AND status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    return conn.execute(f"SELECT * FROM runs WHERE {where} ORDER BY id DESC LIMIT 1", params).fetchone()


def save_feedback(conn: sqlite3.Connection, feedback: dict[str, Any]) -> int:
    values = {**feedback, "created_at": to_iso(feedback["created_at"])}
    with conn:
        cur = conn.execute(
            f"INSERT INTO feedback ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )
    return cur.lastrowid


def feedback_rows(conn: sqlite3.Connection, day: str | None = None) -> list[sqlite3.Row]:
    if day is None:
        return conn.execute("SELECT * FROM feedback ORDER BY id").fetchall()
    return conn.execute("SELECT * FROM feedback WHERE digest_date = ? ORDER BY id", (day,)).fetchall()


def json_field(row: sqlite3.Row | None, name: str) -> Any:
    """JSON из колонки строки; None — строки или значения нет."""
    return json.loads(row[name]) if row is not None and row[name] else None


def merge_run_stats(conn: sqlite3.Connection, run_id: int, extra: dict[str, Any]) -> None:
    """Дополняет stats_json запуска тем, что стало известно после его конца: текст статей, курсы."""
    row = conn.execute("SELECT stats_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    stats = {**(json_field(row, "stats_json") or {}), **extra}
    with conn:
        conn.execute(
            "UPDATE runs SET stats_json = ? WHERE id = ?",
            (mask(json.dumps(stats, ensure_ascii=False)), run_id),
        )


# --- Показатели ---


def runs_since(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    """Запуски, начатые не раньше since, и завершённые, и оборвавшиеся, старые первыми."""
    return conn.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY id", (to_iso(since),)).fetchall()


def first_digest_date(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MIN(date) FROM digests").fetchone()[0]


def digests_since(conn: sqlite3.Connection, since_date: str) -> list[sqlite3.Row]:
    """Дайджесты с даты ГГГГ-ММ-ДД, старые первыми: модель, окно и статистика запуска, который их собрал."""
    return conn.execute(
        """
        SELECT d.date, d.model_json, r.window_from, r.window_to, r.stats_json
        FROM digests d LEFT JOIN runs r ON r.id = d.run_id
        WHERE d.date >= ? AND d.model_json IS NOT NULL
        ORDER BY d.date
        """,
        (since_date,),
    ).fetchall()


def window_counts(
    conn: sqlite3.Connection, since: datetime, until: datetime, late_from: datetime | None = None
) -> sqlite3.Row:
    """Сколько материалов в окне: всего, без заголовка, отсеяно префильтром."""
    return conn.execute(
        f"""
        SELECT COUNT(*) AS collected,
               COALESCE(SUM(i.title = ''), 0) AS no_title,
               COALESCE(SUM(i.prefilter = 'filtered'), 0) AS filtered
        FROM items i WHERE {_IN_WINDOW}
        """,
        _window_params(since, until, late_from),
    ).fetchone()


def unseen_items(conn: sqlite3.Connection, since: datetime, until: datetime) -> list[sqlite3.Row]:
    """Материалы включённых источников из окон [since, until), которых не видела ни одна сборка.

    Собранные после until не считаются: их заберёт следующая сборка.
    """
    return conn.execute(
        """
        SELECT s.code AS source, i.title, i.published_at, i.fetched_at
        FROM items i JOIN sources s ON s.id = i.source_id
        WHERE i.prefilter IS NULL AND i.title != '' AND s.enabled = 1
          AND i.published_at >= ? AND i.published_at < ? AND i.fetched_at < ?
        ORDER BY i.published_at
        """,
        (to_iso(since), to_iso(until), to_iso(until)),
    ).fetchall()


def source_overview(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    """Включённые источники: материалы, собранные с since, сколько из них без текста, последний материал."""
    return conn.execute(
        """
        SELECT s.code, s.name, s.tier, s.last_ok_at, s.last_error,
               COALESCE(SUM(i.fetched_at >= :since), 0) AS items,
               COALESCE(SUM(i.fetched_at >= :since AND (i.text_status = 'failed' OR i.title = '')), 0)
                 AS no_text,
               MAX(i.fetched_at) AS last_item_at
        FROM sources s LEFT JOIN items i ON i.source_id = s.id
        WHERE s.enabled = 1
        GROUP BY s.id
        ORDER BY s.tier, s.code
        """,
        {"since": to_iso(since)},
    ).fetchall()


def posts_since(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM posts WHERE created_at >= ? ORDER BY id", (to_iso(since),)).fetchall()


def feedback_since(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM feedback WHERE created_at >= ? ORDER BY id", (to_iso(since),)
    ).fetchall()
