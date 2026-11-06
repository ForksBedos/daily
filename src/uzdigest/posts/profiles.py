"""Профили площадок (config/posts.yaml) и образцы постов СММ (config/post_examples/{площадка}/*.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from uzdigest.config import CONFIG_DIR
from uzdigest.render.model import Lang

EXAMPLES_DIR = CONFIG_DIR / "post_examples"


class Profile(BaseModel):
    code: str = ""  # ключ в platforms; заполняется при загрузке
    name: str
    langs: list[Lang] = Field(min_length=1)  # первый — язык по умолчанию
    max_chars: int = Field(gt=0)  # жёсткий лимит площадки
    target: tuple[int, int]  # ориентир длины
    hashtags: tuple[int, int]  # от и до
    link: Literal["required", "none"]
    rules: str
    examples: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Profile:
        low, high = self.target
        if not 0 < low <= high <= self.max_chars:
            raise ValueError(f"target {self.target} должен быть внутри лимита {self.max_chars}")
        if not 0 <= self.hashtags[0] <= self.hashtags[1]:
            raise ValueError(f"hashtags {self.hashtags}: нужно «от» ≤ «до»")
        return self

    @property
    def default_lang(self) -> Lang:
        return self.langs[0]


class Brand(BaseModel):
    name: str  # подпись поста
    about: str  # кто пишет: подставляется в промпт
    advice_label: dict[Lang, str]  # заголовок блока рекомендации на языке поста


class Profiles(BaseModel):
    brand: Brand
    tone: str
    platforms: dict[str, Profile]

    def get(self, code: str) -> Profile:
        if code not in self.platforms:
            raise LookupError(f"Нет площадки {code}. Есть: {', '.join(self.platforms)}")
        return self.platforms[code]


def load_profiles(path: Path = CONFIG_DIR / "posts.yaml", examples_dir: Path = EXAMPLES_DIR) -> Profiles:
    profiles = Profiles.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    for code, profile in profiles.platforms.items():
        profile.code = code
        folder = examples_dir / code
        if folder.is_dir():
            profile.examples = [
                text
                for file in sorted(folder.glob("*.md"))
                if (text := file.read_text(encoding="utf-8").strip())
            ]
    return profiles
