"""Команда dedup-report (docs/plan/stage-3-dedup.md, задача 3.6).

data/dedup_pairs.csv — пары-кандидаты с признаками и пустой колонкой same для разметки
(да/нет). Уже проставленные командой отметки при перезаписи файла сохраняются.
data/dedup_clusters.md — кластеры окна для ручной проверки «явных дублей нет».
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from uzdigest.dedup.clustering import Candidate
from uzdigest.dedup.stage import WindowResult
from uzdigest.timeutil import TASHKENT

PAIR_FIELDS = [
    "item_a", "item_b", "source_a", "source_b", "title_a", "title_b",
    "title_sim", "anchors", "text_sim", "score", "decision", "same_doc_key", "same",
]  # fmt: skip


def write_pairs(path: Path, window: WindowResult) -> int:
    labels = read_labels(path)
    by_id = {c.id: c for c in window.candidates}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:  # с BOM — чтобы Excel открыл кириллицу
        writer = csv.DictWriter(f, PAIR_FIELDS, delimiter=";")
        writer.writeheader()
        for pair in window.result.pairs:
            a, b = by_id[pair.a], by_id[pair.b]
            writer.writerow(
                {
                    "item_a": a.id,
                    "item_b": b.id,
                    "source_a": a.source,
                    "source_b": b.source,
                    "title_a": a.title,
                    "title_b": b.title,
                    "title_sim": f"{pair.features.title:.2f}",
                    "anchors": pair.features.anchors,
                    "text_sim": f"{pair.features.text:.2f}",
                    "score": f"{pair.score:.3f}",
                    "decision": pair.override or pair.decision,
                    "same_doc_key": "да" if a.doc_key and a.doc_key == b.doc_key else "",
                    "same": labels.get((a.id, b.id), ""),
                }
            )
    tmp.replace(path)  # размеченный файл не теряется, даже если запись оборвалась
    return len(window.result.pairs)


def read_labels(path: Path) -> dict[tuple[int, int], str]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {
            (int(row["item_a"]), int(row["item_b"])): row["same"].strip()
            for row in csv.DictReader(f, delimiter=";")
            if row.get("same", "").strip()
        }


def write_clusters(path: Path, window: WindowResult) -> None:
    by_id = {c.id: c for c in window.candidates}
    clusters = window.result.clusters
    multi = [cl for cl in clusters if len(cl.item_ids) > 1]
    lines = [
        "# Кластеры окна",
        "",
        f"Материалов после префильтра: {len(window.candidates)}, отсеяно: {len(window.filtered)}.",
        f"Сюжетов: {len(clusters)}, из них с дублями: {len(multi)}, правовых: "
        f"{sum(cl.kind == 'legal' for cl in clusters)}. Пар в серой зоне: {len(window.result.gray)}.",
        "",
        "## Сюжеты с дублями",
        "",
    ]
    for cl in sorted(multi, key=lambda c: -len(c.item_ids)):
        lines.append(f"### {by_id[cl.primary_id].title}")
        if cl.doc_key:
            lines.append(f"`{cl.doc_key}`")
        lines += [f"- {_line(by_id[i])}" for i in cl.item_ids]
        lines.append("")
    lines += ["## Серая зона (решит Claude на этапе 5)", ""]
    for pair in window.result.gray:
        lines.append(f"- {pair.score:.2f}: {_line(by_id[pair.a])} ⟷ {_line(by_id[pair.b])}")
    reasons = Counter(r.split(":", 1)[0] for r in window.filtered.values())
    lines += ["", "## Отсеяно префильтром", "", *(f"- {k}: {v}" for k, v in reasons.most_common())]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _line(c: Candidate) -> str:
    when = c.published_at.astimezone(TASHKENT).strftime("%d.%m %H:%M")
    return f"[{c.source}] {c.title} ({when}, id {c.id})"
