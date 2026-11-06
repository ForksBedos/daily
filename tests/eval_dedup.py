"""Точность и полнота дедупа на размеченных парах (docs/plan/stage-3-dedup.md, задача 3.6).

Запуск из корня проекта:
    .venv/Scripts/python.exe tests/eval_dedup.py [data/dedup_pairs.csv]

Файл создаёт `uzdigest dedup-report`; команда заполняет колонку same (да/нет). Скрипт пересчитывает
оценку по признакам из файла при разных весах и порогах и печатает, при каких значениях
правила «объединить» дают точность ≥ 95 %. Серая зона показана отдельно: её решает Claude.
В файл попадают только пары с оценкой от 0,15, поэтому полнота считается среди них.
"""

from __future__ import annotations

import csv
import itertools
import sys
from pathlib import Path

YES = {"да", "yes", "y", "1", "+", "д"}
NO = {"нет", "no", "n", "0", "-", "н"}
TARGET_PRECISION = 0.95


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    labeled = []
    for row in rows:
        label = row["same"].strip().lower()
        if label in YES or label in NO:
            labeled.append(
                {
                    "title": float(row["title_sim"]),
                    "anchors": int(row["anchors"]),
                    "text": float(row["text_sim"]),
                    "doc_key": row["same_doc_key"] == "да",
                    "same": label in YES,
                }
            )
    return labeled


def score(pair: dict, w_title: float, w_anchors: float, w_text: float, cap: int = 3) -> float:
    return w_title * pair["title"] + w_anchors * min(pair["anchors"], cap) / cap + w_text * pair["text"]


def evaluate(pairs: list[dict], weights: tuple[float, float, float], merge: float, gray: float) -> dict:
    tp = fp = fn = gray_count = 0
    for pair in pairs:
        s = score(pair, *weights)
        merged = pair["doc_key"] or s >= merge
        gray_count += not merged and s >= gray
        tp += merged and pair["same"]
        fp += merged and not pair["same"]
        fn += not merged and pair["same"]
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": precision, "recall": recall, "gray": gray_count}


def main(path: Path) -> int:
    pairs = load(path)
    positives = sum(p["same"] for p in pairs)
    if not pairs:
        print(f"В {path} нет размеченных пар: заполните колонку same (да/нет).")
        return 1
    print(f"Размечено пар: {len(pairs)}, из них дублей: {positives}")

    print("\nТекущие веса 0.40 / 0.25 / 0.35, серая зона от 0.22:")
    print(f"{'порог':>6} {'точность':>9} {'полнота':>8} {'серая':>6}")
    for merge in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        r = evaluate(pairs, (0.40, 0.25, 0.35), merge, 0.22)
        print(f"{merge:>6.2f} {r['precision']:>9.1%} {r['recall']:>8.1%} {r['gray']:>6}")

    best = []
    grid = [0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5]
    for w_title, w_anchors in itertools.product(grid, grid):
        w_text = round(1 - w_title - w_anchors, 2)
        if w_text < 0.1:
            continue
        for merge in [x / 100 for x in range(30, 66, 5)]:
            r = evaluate(pairs, (w_title, w_anchors, w_text), merge, 0.22)
            if r["precision"] >= TARGET_PRECISION:
                best.append((r["recall"], -r["gray"], w_title, w_anchors, w_text, merge, r["precision"]))
    print(f"\nЛучшие веса при точности правил ≥ {TARGET_PRECISION:.0%}:")
    print(f"{'title':>6} {'anchors':>8} {'text':>5} {'порог':>6} {'точность':>9} {'полнота':>8}")
    for recall, _, w_title, w_anchors, w_text, merge, precision in sorted(best, reverse=True)[:8]:
        print(
            f"{w_title:>6.2f} {w_anchors:>8.2f} {w_text:>5.2f} {merge:>6.2f} {precision:>9.1%} {recall:>8.1%}"
        )
    return 0


if __name__ == "__main__":
    default = Path(__file__).resolve().parents[1] / "data" / "dedup_pairs.csv"
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else default))
