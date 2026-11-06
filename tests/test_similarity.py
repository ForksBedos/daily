"""Похожесть и кластеры на реальных материалах 30.10–02.11.2026 (tests/fixtures/dedup_items.json).

Пороги предварительные, до разметки командой (docs/plan/stage-3-dedup.md, задача 3.6).
"""

import itertools
import json
from datetime import datetime

import pytest
from conftest import fixture_bytes

from uzdigest.config import DedupSettings
from uzdigest.dedup.clustering import Candidate, build_clusters
from uzdigest.dedup.similarity import compare, decide, fingerprint
from uzdigest.legal.doc_key import find_refs

DATA = json.loads(fixture_bytes("dedup_items.json"))
ITEMS = {item["id"]: item for item in DATA["items"]}
GROUPS = DATA["groups"]
SETTINGS = DedupSettings()


def features(a, b):
    first, second = ITEMS[a], ITEMS[b]
    return compare(fingerprint(first["title"], first["text"]), fingerprint(second["title"], second["text"]))


def candidates(ids=None):
    result = []
    for item in DATA["items"]:
        if ids is not None and item["id"] not in ids:
            continue
        published_at = datetime.fromisoformat(item["published_at"])
        refs = find_refs(f"{item['title']}\n{item['text']}", published_at.year)
        result.append(
            Candidate(
                id=item["id"],
                source=item["source"],
                tier=item["tier"],
                title=item["title"],
                text=item["text"],
                url=item["url"],
                published_at=published_at,
                refs=tuple(refs),
            )
        )
    return result


def cluster_of(result, item_id):
    return next(set(c.item_ids) for c in result.clusters if item_id in c.item_ids)


def test_imarat_development_in_six_outlets_is_one_cluster():
    imarat = GROUPS["imarat"]
    result = build_clusters(candidates(), SETTINGS)
    assert cluster_of(result, imarat[0]) >= set(imarat)


def test_imarat_pairs_are_never_called_different():
    for a, b in itertools.combinations(GROUPS["imarat"], 2):
        assert decide(features(a, b), SETTINGS) != "different"


def test_museums_and_us_ai_are_not_merged_despite_shared_words():
    # «Музеи Узбекистана и Петербурга договорились о сотрудничестве» и
    # «Узбекистан и США договорились о сотрудничестве в сфере ИИ»: символьная похожесть 0,63.
    # Правила их не объединяют; серая зона — значит, окончательно решит Claude
    [museums], [usa] = GROUPS["museums"], GROUPS["usa_ai"]
    assert decide(features(museums, usa), SETTINGS) != "same"
    result = build_clusters(candidates([museums, usa]), SETTINGS)
    assert museums not in cluster_of(result, usa)


@pytest.mark.parametrize(("a", "b"), [(71, 179), (59, 201), (17, 59), (59, 71)])
def test_mobile_radars_paraphrases_are_gray_not_different(a, b):
    # «В Узбекистане разрешили не предупреждать водителей о мобильных радарах» и
    # «В Узбекистане отменили знаки перед мобильными радарами» — без общих слов в заголовке
    assert decide(features(a, b), SETTINGS) == "gray"


def test_mobile_radars_merge_by_legal_key_and_rest_go_to_gray_zone():
    radar = GROUPS["radar"]
    result = build_clusters(candidates(), SETTINGS)
    legal = next(c for c in result.clusters if c.doc_key == "PKM-596-2026")
    assert legal.kind == "legal"
    # ПКМ № 596 упомянут в начале текста у kun, uzdaily и podrobno; у остальных номера нет
    assert {17, 179, 312} <= set(legal.item_ids)
    gray_items = {i for p in result.gray for i in (p.a, p.b)}
    assert {59, 71, 201} <= gray_items
    assert set(radar) - set(legal.item_ids) <= gray_items


def test_gas_stations_in_nukus_are_gray_until_claude_decides():
    # «В Нукусе оштрафованы 22 газонаполнительные станции» и «Комитет по конкуренции оштрафовал
    # 22 газовые заправки»: общие только «Нукус» и «22»
    for a, b in itertools.combinations(GROUPS["gas"], 2):
        assert decide(features(a, b), SETTINGS) in ("gray", "same")


def test_claude_decision_merges_gray_pair():
    a, b = sorted(GROUPS["gas"][:2])
    result = build_clusters(candidates(), SETTINGS, decisions={(a, b): "same"})
    assert cluster_of(result, a) >= {a, b}
    assert all({p.a, p.b} != {a, b} for p in result.gray)


def test_different_story_about_same_case_stays_apart():
    # Жена владельца поддержала мужа — другой сюжет, хотя якоря те же
    result = build_clusters(candidates(), SETTINGS)
    assert GROUPS["wife"][0] not in cluster_of(result, GROUPS["imarat"][0])


def test_anchors_skip_dates_and_common_words():
    print_a = fingerprint("Постановление Кабинета Министров от 29 октября 2026 года", "")
    print_b = fingerprint("Кабинет Министров 29 октября 2026 года утвердил", "")
    assert not print_a.anchors & print_b.anchors
    imarat = fingerprint("Дело против Imarat Development", "Ущерб 187 млрд сумов, задержан Марат Хамдамов.")
    assert {"imarat", "development", "#187", "марат", "хамда"} <= imarat.anchors
