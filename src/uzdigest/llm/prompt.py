"""Системные промпты из llm/prompts/*.md. Имя файла — версия, она пишется в БД с каждым результатом."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from typing import Any


@dataclass(frozen=True)
class Prompt:
    version: str  # triage_v1
    text: str

    def fill(self, **values: str) -> str:
        """Подставляет {{имя}}: фигурные скобки в примерах JSON остаются как есть."""
        text = self.text
        for name, value in values.items():
            text = text.replace("{{" + name + "}}", value)
        return text


def load_prompt(version: str) -> Prompt:
    path = resources.files("uzdigest.llm") / "prompts" / f"{version}.md"
    return Prompt(version, path.read_text(encoding="utf-8"))


def input_hash(*parts: Any) -> str:
    """Отпечаток входа вызова: изменился текст, реквизиты или промпт — кэшированный ответ устарел."""
    data = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def as_json(data: Any) -> str:
    """Данные для промпта: JSON без экранирования кириллицы."""
    return json.dumps(data, ensure_ascii=False, indent=1)
