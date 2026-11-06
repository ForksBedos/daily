import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from uzdigest.config import DedupSettings, Source
from uzdigest.db import repo
from uzdigest.dedup import report
from uzdigest.dedup.clustering import Candidate, build_clusters, find_repeats, save_clusters
from uzdigest.dedup.stage import prepare_window
from uzdigest.extract.normalize import to_item
from uzdigest.filter.prefilter import Keywords
from uzdigest.legal.doc_key import find_refs
from uzdigest.models import RawItem

SETTINGS = DedupSettings()
T0 = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)

IMARAT = "В Ташкенте возбудили уголовное дело против застройщика Imarat Development"
IMARAT_TEXT = (
    "ГУВД Ташкента возбудило уголовное дело против руководства Imarat Development после жалоб дольщиков."
)


def cand(id, source, title, text="", hours=0.0, tier="C"):
    published_at = T0 + timedelta(hours=hours)
    refs = find_refs(f"{title}\n{text}", published_at.year)
    return Candidate(id, source, tier, title, text, f"https://{source}.uz/{id}", published_at, tuple(refs))


def test_primary_is_earliest_then_higher_tier():
    result = build_clusters(
        [
            cand(1, "gazeta", IMARAT, IMARAT_TEXT, hours=2),
            cand(2, "daryo", IMARAT, IMARAT_TEXT, hours=1),
            cand(3, "soliq", IMARAT, IMARAT_TEXT, hours=1, tier="A"),
        ],
        SETTINGS,
    )
    [cluster] = result.clusters
    assert cluster.item_ids == [3, 2, 1]  # 3 и 2 вышли одновременно, у 3 уровень A
    assert cluster.kind == "news" and cluster.doc_key is None


def test_legal_cluster_from_different_titles():
    norma = "Постановлением Кабмина от 28.10.2026 г. № 594 утвержден порядок подготовки кадров."
    uzdaily = "Кабинет Министров принял постановление № 594 от 28.10.2026 г. о подготовке кадров."
    result = build_clusters(
        [
            cand(
                1, "norma", "Специалисты по кибербезопасности будут проходить повышение квалификации", norma
            ),
            cand(2, "uzdaily", "Утверждён порядок подготовки кадров в сфере защиты информации", uzdaily, 5),
            cand(3, "kun", "Музеи Ташкента продлят часы работы", "Режим изменится с понедельника.", 1),
        ],
        SETTINGS,
    )
    legal = next(c for c in result.clusters if c.kind == "legal")
    assert legal.doc_key == "PKM-594-2026" and legal.item_ids == [1, 2]
    assert len(result.clusters) == 2


def test_passing_mentions_and_reviews_do_not_link():
    filler = "Подробности сообщили в ведомстве. " * 30
    context = cand(
        1,
        "uzdaily",
        "Объём биржевых сделок вырос на 29%",
        f"{filler}Ранее было принято постановление Кабмина № 82 от 10.02.2026 г.",
    )
    main = cand(
        2,
        "kun",
        "Утверждён порядок торговли фьючерсами",
        "Постановлением Кабмина от 10.02.2026 г. № 82 утверждён порядок торговли.",
        hours=1,
    )
    old_act = cand(
        3,
        "spot",
        "Частным компаниям разрешат фиксировать нарушения",
        "Изменения внесены в ПКМ № 232 от 12.04.2021 г.",
        hours=2,
    )
    review = cand(
        4,
        "gazeta",
        "Что изменится с 1 ноября",
        "УП-236 от 28.10.2026, ПП-371 от 13.10.2026, ПП-387 от 27.10.2026 и ПКМ № 82 от 10.02.2026 г.",
        hours=3,
    )
    assert context.doc_key is None  # упоминание дальше начала текста
    assert main.doc_key == "PKM-82-2026"
    assert old_act.doc_key is None  # акт 2021 года — к слову, а не о нём заметка
    assert review.doc_key is None and len(review.refs) == 4
    result = build_clusters([context, main, old_act, review], SETTINGS)
    assert len(result.clusters) == 4
    assert old_act.refs[0].doc_key == "PKM-232-2021"  # в item_legal_refs ключ всё равно попадает
    # fmt: skip


def test_same_source_and_distant_items_are_not_compared():
    result = build_clusters(
        [
            cand(1, "uzdaily", IMARAT, IMARAT_TEXT),
            cand(2, "uzdaily", IMARAT, IMARAT_TEXT, hours=1),
            cand(3, "daryo", IMARAT, IMARAT_TEXT, hours=100),
        ],
        SETTINGS,
    )
    assert len(result.clusters) == 3 and result.pairs == []


def test_decision_overrides_rules():
    items = [cand(1, "gazeta", IMARAT, IMARAT_TEXT), cand(2, "daryo", IMARAT, IMARAT_TEXT, hours=1)]
    assert len(build_clusters(items, SETTINGS).clusters) == 1
    result = build_clusters(items, SETTINGS, decisions={(1, 2): "different"})
    assert len(result.clusters) == 2
    assert result.pairs[0].override == "different" and not result.pairs[0].same


def setup_db(rows):
    """rows: (код, RawItem). Источники — kun, daryo (C) и soliq (A)."""
    sources = [
        Source(code="kun", name="kun", kind="rss", url="https://kun.uz/rss", tier="C"),
        Source(code="daryo", name="daryo", kind="rss", url="https://daryo.uz/rss", tier="C"),
        Source(code="soliq", name="soliq", kind="rss", url="https://soliq.uz/rss", tier="A"),
    ]
    conn = repo.connect(":memory:")
    ids = repo.sync_sources(conn, sources)
    run_id = repo.start_run(conn, "collect")
    with conn:
        for code, raw in rows:
            repo.insert_item(conn, ids[code], run_id, to_item(raw, "ru", T0 + timedelta(hours=10)))
    item_ids = {row["url"]: row["id"] for row in conn.execute("SELECT id, url FROM items")}
    return conn, sources, item_ids


ROWS = [
    ("kun", RawItem("https://kun.uz/1", IMARAT, None, IMARAT_TEXT, None, T0)),
    ("daryo", RawItem("https://daryo.uz/1", IMARAT, None, IMARAT_TEXT, None, T0 + timedelta(hours=1))),
    ("daryo", RawItem("https://daryo.uz/2", "Хусанов отыграл весь матч за Манчестер Сити", None,
                      "Матч завершился поражением.", None, T0)),
    ("soliq", RawItem("https://soliq.uz/1", "Разъяснения по возврату НДС", None,
                      "В соответствии с Указом Президента от 4.09.2025 г. № УП-153 возврат НДС производится "
                      "автоматически.", None, T0)),
]  # fmt: skip


def window(conn, sources):
    return prepare_window(
        conn, sources, Keywords.load(), SETTINGS, T0 - timedelta(hours=1), T0 + timedelta(hours=9)
    )


def test_prepare_window_stores_prefilter_and_refs():
    conn, sources, ids = setup_db(ROWS)
    result = window(conn, sources)
    assert set(result.filtered) == {ids["https://daryo.uz/2"]}
    assert len(result.candidates) == 3 and len(result.result.clusters) == 2
    football = conn.execute("SELECT prefilter, prefilter_reason FROM items WHERE id = ?",
                            (ids["https://daryo.uz/2"],)).fetchone()  # fmt: skip
    assert (football["prefilter"], football["prefilter_reason"]) == ("filtered", "topic:матч")
    ref = conn.execute("SELECT * FROM item_legal_refs").fetchone()
    assert (ref["doc_key"], ref["is_draft"], ref["year_guessed"]) == ("UP-153-2025", 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 0  # отчёт кластеры не пишет


def test_save_clusters_keeps_ids_and_later_statuses():
    conn, sources, ids = setup_db(ROWS)
    first = window(conn, sources)
    cluster_ids = save_clusters(conn, first.result, list(first.filtered), T0)
    statuses = {row["url"]: (row["status"], row["cluster_id"]) for row in
                conn.execute("SELECT url, status, cluster_id FROM items")}  # fmt: skip
    imarat = cluster_ids[[i for i, c in enumerate(first.result.clusters) if len(c.item_ids) == 2][0]]
    assert statuses["https://kun.uz/1"] == ("primary", imarat)
    assert statuses["https://daryo.uz/1"] == ("duplicate", imarat)
    assert statuses["https://daryo.uz/2"] == ("filtered", None)

    with conn:
        conn.execute("UPDATE items SET status = 'published' WHERE url = 'https://kun.uz/1'")
    second = window(conn, sources)
    assert save_clusters(conn, second.result, list(second.filtered), T0) == cluster_ids
    assert conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 2
    assert (
        conn.execute("SELECT status FROM items WHERE url = 'https://kun.uz/1'").fetchone()[0] == "published"
    )


def test_repeats_against_previous_digests():
    conn, sources, ids = setup_db(ROWS)
    first = window(conn, sources)
    cluster_ids = save_clusters(conn, first.result, list(first.filtered), T0)
    with conn:
        conn.execute("INSERT INTO digests (date, dir, created_at) VALUES ('2026-11-02', 'x', 'now')")
        conn.executemany(
            "INSERT INTO digest_entries (digest_id, cluster_id, position) VALUES (1, ?, ?)",
            [(cid, n) for n, cid in enumerate(cluster_ids)],
        )
    # Тот же день повторно: оба кластера уже выходили — правовой узнаётся по ключу, второй по материалу
    second = window(conn, sources)
    repeats = find_repeats(conn, second.result, second.candidates, SETTINGS, date(2026, 11, 3))
    assert sorted(r.reason for r in repeats.values()) == ["doc_key", "item"]

    # Новый материал о том же деле в другом издании — повтор по похожести
    later = [cand(99, "spot", IMARAT, IMARAT_TEXT, hours=30)]
    result = build_clusters(later, SETTINGS)
    [repeat] = find_repeats(conn, result, later, SETTINGS, date(2026, 11, 3)).values()
    assert repeat.reason == "similar"
    # Через 14 дней — уже не повтор
    assert find_repeats(conn, result, later, SETTINGS, date(2026, 11, 20)) == {}


def test_pair_decisions_priority():
    conn, _, ids = setup_db(ROWS)
    a, b = ids["https://kun.uz/1"], ids["https://daryo.uz/1"]
    with conn:
        repo.save_pair_decision(conn, b, a, "same", "llm")
        repo.save_pair_decision(conn, a, b, "different", "manual")
    assert repo.pair_decisions(conn, [a, b]) == {(min(a, b), max(a, b)): "different"}


def test_report_keeps_team_labels(tmp_path):
    conn, sources, _ = setup_db(ROWS)
    result = window(conn, sources)
    path = tmp_path / "dedup_pairs.csv"
    assert report.write_pairs(path, result) == 1
    text = path.read_text(encoding="utf-8-sig")
    path.write_text(text.rstrip("\r\n") + "да\n", encoding="utf-8-sig")  # команда отметила пару
    report.write_pairs(path, result)
    assert list(report.read_labels(path).values()) == ["да"]
    report.write_clusters(tmp_path / "clusters.md", result)
    md = (tmp_path / "clusters.md").read_text(encoding="utf-8")
    assert IMARAT in md and "topic: 1" in md


# --- Дубли без общих слов в заголовке: настоящие статьи выпуска 05.11 ---


def _digest_20261105() -> dict[int, Candidate]:
    rows = json.loads((Path(__file__).parent / "fixtures/dedup/digest_20261105.json").read_text("utf-8"))
    return {
        row["position"]: Candidate(
            row["id"],
            row["source"],
            "C",
            row["title"],
            row["text"],
            row["url"],
            T0 + timedelta(minutes=row["position"]),
        )
        for row in rows
    }


def _gray_pairs(items: dict[int, Candidate]) -> set[tuple[int, int]]:
    by_item = {c.id: position for position, c in items.items()}
    result = build_clusters(list(items.values()), SETTINGS)
    return {tuple(sorted((by_item[p.a], by_item[p.b]))) for p in result.gray}


def test_same_law_under_unlike_headlines_goes_to_claude():
    """ЗРУ-1186: «Газовые заправки будут штрафовать…» и «…нарушения пожарной безопасности»."""
    assert (2, 3) in _gray_pairs(_digest_20261105())


def test_same_vat_order_retold_goes_to_claude():
    assert (5, 6) in _gray_pairs(_digest_20261105())


def test_unrelated_articles_do_not_go_to_claude():
    pairs = _gray_pairs(_digest_20261105())
    assert (2, 5) not in pairs  # пожарная безопасность и зачёт НДС
    assert (18, 21) not in pairs  # Бухарский НПЗ и дата-центр


def test_text_overlap_alone_never_merges():
    """Совпадение текста только зовёт Claude: без его ответа это разные сюжеты."""
    items = _digest_20261105()
    result = build_clusters([items[2], items[3]], SETTINGS.model_copy(update={"text_gray_threshold": 0.0}))
    assert len(result.clusters) == 2


# В образце 25 статей, а не 300 материалов окна: редкие слова весят меньше, порог ниже рабочего
SMALL_SAMPLE = SETTINGS.model_copy(update={"repeat_text_threshold": 0.5})


def _published(items: dict[int, Candidate]) -> list[dict]:
    return [
        {"id": 1000 + n, "doc_key": None, "primary_item_id": c.id, "title": c.title, "text": c.text}
        for n, c in items.items()
    ]


def test_retold_news_from_earlier_digest_is_a_repeat(monkeypatch):
    """Зачёт НДС вышел вчера у kun.uz, сегодня spot.uz пересказал его другими словами."""
    items = _digest_20261105()
    new = items.pop(6)
    monkeypatch.setattr(repo, "published_clusters", lambda *args: _published(items))
    result = build_clusters([new], SETTINGS)
    [repeat] = find_repeats(None, result, [new], SMALL_SAMPLE, date(2026, 11, 6)).values()
    assert (repeat.previous_cluster_id, repeat.reason) == (1005, "text")


def test_new_news_on_the_same_day_topic_is_not_a_repeat(monkeypatch):
    """Заседание ЦБ и статистика капитала банков: темы рядом, новости разные."""
    items = _digest_20261105()
    new = items.pop(13)
    monkeypatch.setattr(repo, "published_clusters", lambda *args: _published(items))
    result = build_clusters([new], SETTINGS)
    assert find_repeats(None, result, [new], SMALL_SAMPLE, date(2026, 11, 6)) == {}
