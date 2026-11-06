"""Посты для соцсетей по статьям дайджеста (docs/plan/stage-7-posts.md).

Тема — статья дайджеста дня (модель из digests.model_json, та же, что digest.json). Пост — один вызов
Claude по профилю площадки из config/posts.yaml. Проверка (posts/qa.py) не прошла — один повтор
с перечнем ошибок; снова не прошла — пост отдаётся с пометкой «⚠️ проверить» и причиной.
«Тот же пост» с тем же входом берётся из БД без Claude; другой вариант, короче и длиннее — новый вызов.
Пост по голосовому проходит тот же путь, только вместо статьи — расшифровка (make_voice_post).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel

from uzdigest import voice
from uzdigest.config import Settings
from uzdigest.db import repo
from uzdigest.llm.client import LlmBudget
from uzdigest.llm.prompt import as_json, input_hash, load_prompt
from uzdigest.llm.schemas import PostResponse
from uzdigest.llm.translate import Glossary
from uzdigest.posts import qa
from uzdigest.posts.profiles import Profile, Profiles, load_profiles
from uzdigest.render.markdown import write_files
from uzdigest.render.model import LANGS, Article, Digest, Text
from uzdigest.timeutil import TASHKENT, tashkent_date, tashkent_to_utc, utc_now

PROMPT = "post_v2"
VOICE_PROMPT = "post_voice_v1"
Mode = Literal["new", "shorter", "longer", "variant"]
MODES: tuple[Mode, ...] = ("new", "shorter", "longer", "variant")
RU_RULES = "- Пиши по-русски, как пишут деловые СМИ Узбекистана (Газета.uz, Kun.uz, Spot)."


class PostError(Exception):
    """Пост не сделан: причина — в тексте, его можно показать пользователю как есть."""


class PostLimitReached(PostError):
    pass


class Topic(BaseModel):
    """Тема для выбора: статья дайджеста. Будущая кнопка в боте (этап 10)."""

    id: str  # 2026-11-03-002
    number: str  # 002
    category: str
    headline: str  # на русском
    importance: int
    doc_key: str | None


class Post(BaseModel):
    id: int
    digest_date: date
    article_id: str
    platform: str
    lang: str
    mode: str
    text: str
    chars: int
    max_chars: int
    hashtags: list[str]
    notes: str
    flags: list[str]  # непустой — «⚠️ проверить»
    path: str
    calls: int  # вызовов Claude на этот ответ; 0 — взят из БД
    cached: bool


def load_digest(conn: sqlite3.Connection, day: date | None = None) -> Digest:
    """Дайджест за день, по умолчанию — последний собранный."""
    key = day.isoformat() if day else repo.latest_digest_date(conn)
    row = repo.digest_by_date(conn, key) if key else None
    if row is None or not row["model_json"]:
        raise LookupError(f"Дайджеста за {key or 'какой-либо день'} нет: сначала uzdigest run")
    return Digest.from_json(row["model_json"])


def list_topics(digest: Digest) -> list[Topic]:
    """Статьи дайджеста в его порядке: по разделам, внутри — по важности."""
    return [
        Topic(
            id=a.id,
            number=a.id.rsplit("-", 1)[1],
            category=a.category,
            headline=a.texts["ru"].headline,
            importance=a.importance,
            doc_key=a.doc.doc_key if a.doc else None,
        )
        for a in digest.articles
    ]


def find_article(digest: Digest, ref: str) -> Article:
    """Статья по полному id (2026-11-03-002) или по номеру в дайджесте (002 или 2)."""
    ref = ref.strip()
    for article in digest.articles:
        number = article.id.rsplit("-", 1)[1]
        if ref == article.id or (ref.isdigit() and int(ref) == int(number)):
            return article
    raise LookupError(f"В дайджесте за {digest.date.isoformat()} нет статьи {ref}: см. uzdigest topics")


def make_post(
    conn: sqlite3.Connection,
    settings: Settings,
    digest: Digest,
    article_ref: str,
    platform: str,
    lang: str | None = None,
    mode: Mode = "new",
    *,
    author: str | None = None,
    budget: LlmBudget | None = None,
    profiles: Profiles | None = None,
    glossary: Glossary | None = None,
    now: datetime | None = None,
) -> Post:
    profiles = profiles or load_profiles()
    profile, lang = _order(profiles, platform, lang, mode)
    article = find_article(digest, article_ref)
    day = digest.date.isoformat()
    system_prompt = _system_prompt(profile, profiles, lang, glossary or Glossary.load())
    earlier = repo.posts_for(conn, day, article.id, platform, lang)
    previous = earlier[-1]["text"] if earlier and mode != "new" else None
    draft = _Draft(
        digest_date=day, item_id=article.id, platform=platform, profile=profile, lang=lang, mode=mode,
        system_prompt=system_prompt, request=_request(article, lang, mode, profile, previous),
        earlier=earlier, sources=_fact_sources(article, lang), allowed=_allowed_links(article),
        advice=profiles.brand.advice_label[lang], prompt=PROMPT, voice=False,
    )  # fmt: skip
    return _write(conn, settings, draft, author, budget, now)


def make_voice_post(
    conn: sqlite3.Connection,
    settings: Settings,
    voice_id: str,
    transcript: str,
    platform: str,
    lang: str | None = None,
    mode: Mode = "new",
    *,
    author: str | None = None,
    budget: LlmBudget | None = None,
    profiles: Profiles | None = None,
    glossary: Glossary | None = None,
    now: datetime | None = None,
) -> Post:
    """Пост по расшифровке голосового. Ссылка и блок рекомендаций не требуются: в источнике их нет."""
    profiles = profiles or load_profiles()
    profile, lang = _order(profiles, platform, lang, mode)
    day, item_id = voice.day_of(voice_id).isoformat(), f"voice-{voice_id}"
    system_prompt = _system_prompt(profile, profiles, lang, glossary or Glossary.load(), VOICE_PROMPT)
    earlier = repo.posts_for(conn, day, item_id, platform, lang)
    previous = earlier[-1]["text"] if earlier and mode != "new" else None
    draft = _Draft(
        digest_date=day, item_id=item_id, platform=platform, profile=profile, lang=lang, mode=mode,
        system_prompt=system_prompt, request=_voice_request(transcript, mode, profile, previous),
        earlier=earlier, sources=[(transcript, qa.detect_lang(transcript) or lang)], allowed=set(),
        advice="", prompt=VOICE_PROMPT, voice=True,
    )  # fmt: skip
    return _write(conn, settings, draft, author, budget, now)


def _order(profiles: Profiles, platform: str, lang: str | None, mode: Mode) -> tuple[Profile, str]:
    if mode not in MODES:
        raise ValueError(f"Режим {mode}: есть {', '.join(MODES)}")
    profile = profiles.get(platform)
    lang = lang or profile.default_lang
    if lang not in LANGS:
        raise ValueError(f"Язык {lang}: есть {', '.join(LANGS)}")
    return profile, lang


@dataclass(frozen=True)
class _Draft:
    """Заказ на пост: всё, чем пост по статье отличается от поста по голосовому."""

    digest_date: str
    item_id: str  # article_id в таблице posts
    platform: str
    profile: Profile
    lang: str
    mode: Mode
    system_prompt: str
    request: str
    earlier: list[sqlite3.Row]
    sources: list[tuple[str, str]]
    allowed: set[str]
    advice: str
    prompt: str
    voice: bool


def _write(
    conn: sqlite3.Connection,
    settings: Settings,
    draft: _Draft,
    author: str | None,
    budget: LlmBudget | None,
    now: datetime | None,
) -> Post:
    """Кэш, дневной лимит, вызов Claude, проверка с одним повтором, запись в файл и в базу."""
    platform, profile, lang, mode = draft.platform, draft.profile, draft.lang, draft.mode
    system_prompt, request, earlier = draft.system_prompt, draft.request, draft.earlier
    key = input_hash(system_prompt, request)
    if mode == "new":
        same = next((row for row in reversed(earlier) if row["input_hash"] == key), None)
        if same is not None:
            return _from_row(same, profile, cached=True)

    now = now or utc_now()
    midnight = tashkent_to_utc(datetime.combine(tashkent_date(now), time()))
    if repo.count_posts_since(conn, midnight) >= settings.posts.max_per_day:
        raise PostLimitReached(
            f"Сегодня сделано уже {settings.posts.max_per_day} постов — дневной лимит (posts.max_per_day)"
        )
    budget = budget or LlmBudget(settings.llm)
    calls_before, usage_before, ms_before = budget.calls, Counter(budget.usage), budget.duration_ms
    answer = budget.ask(request, PostResponse, system_prompt, "post")
    if answer is None:
        raise PostError(f"Claude не ответил: {budget.stopped or 'вызов не удался, попробуйте ещё раз'}")

    def problems(text: str) -> list[str]:
        return qa.check(text, lang, profile, draft.sources, draft.allowed, draft.advice, voice=draft.voice)

    flags = problems(answer.text.strip())
    if flags:
        retry = budget.ask(
            f"{request}\n\n{_fix_request(answer.text, flags)}", PostResponse, system_prompt, "post"
        )
        if retry is not None:
            answer = retry
            flags = problems(answer.text.strip())

    text = answer.text.strip()
    chars = qa.count_chars(text)
    name = f"{draft.item_id}-{platform}-{lang}-{len(earlier) + 1}.md"
    out_dir = settings.outputs_dir / draft.digest_date / "posts"
    header = " · ".join(
        [draft.item_id, profile.name, lang, mode, f"{chars}/{profile.max_chars}", "нужна проверка эксперта"]
    )
    if flags:
        header += f" · ⚠️ проверить: {'; '.join(flags)}"
    write_files(out_dir, {name: f"<!-- {header} -->\n\n{text}\n"})
    post_id = repo.save_post(
        conn,
        {
            "created_at": now,
            "digest_date": draft.digest_date,
            "article_id": draft.item_id,
            "platform": platform,
            "lang": lang,
            "mode": mode,
            "text": text,
            "chars": chars,
            "hashtags": answer.hashtags,
            "notes": answer.notes.strip(),
            "qa_flags": flags,
            "prompt_version": draft.prompt,
            "input_hash": key,
            "model": budget.settings.model,
            "calls": budget.calls - calls_before,
            "author": author,
            "path": str(out_dir / name),
            "usage_json": json.dumps(budget.usage - usage_before),
            "duration_ms": budget.duration_ms - ms_before,
        },
    )
    return _from_row(repo.post(conn, post_id), profile, cached=False)


def _from_row(row: sqlite3.Row, profile: Profile, *, cached: bool) -> Post:
    return Post(
        id=row["id"],
        digest_date=date.fromisoformat(row["digest_date"]),
        article_id=row["article_id"],
        platform=row["platform"],
        lang=row["lang"],
        mode=row["mode"],
        text=row["text"],
        chars=row["chars"],
        max_chars=profile.max_chars,
        hashtags=json.loads(row["hashtags"] or "[]"),
        notes=row["notes"] or "",
        flags=json.loads(row["qa_flags"] or "[]"),
        path=row["path"] or "",
        calls=0 if cached else row["calls"],
        cached=cached,
    )


# --- Промпт ---


def _system_prompt(
    profile: Profile, profiles: Profiles, lang: str, glossary: Glossary, prompt: str = PROMPT
) -> str:
    low, high = profile.hashtags
    if lang == "ru":
        language, rules, terms = "русский", RU_RULES, "Пост на русском: глоссарий не нужен."
    else:
        language = glossary.languages[lang]["name"]
        rules = glossary.languages[lang]["rules"].strip()
        terms = "\n".join(f"- {term['ru']} → {term[lang]}" for term in glossary.terms)
    examples = "\n\n".join(f"### Образец {n}\n\n{text}" for n, text in enumerate(profile.examples, 1))
    return load_prompt(prompt).fill(
        platform=profile.name,
        brand=profiles.brand.name,
        about=profiles.brand.about.strip(),
        advice_label=profiles.brand.advice_label[lang],
        language=language,
        target=f"{profile.target[0]}–{profile.target[1]}",
        max_chars=str(profile.max_chars),
        hashtags="не нужны" if high == 0 else f"от {low} до {high}",
        link=(
            "обязательна ссылка на материал (`source.url`); у правовой статьи можно добавить `doc.lex_url`."
            if profile.link == "required"
            else "адресов в тексте быть не должно: на этой площадке они не кликабельны."
        ),
        platform_rules=profile.rules.strip(),
        tone=profiles.tone.strip(),
        examples=examples or "Образцов пока нет: пиши по правилам выше.",
        language_rules=rules,
        glossary=terms,
    )


def _request(article: Article, lang: str, mode: Mode, profile: Profile, previous: str | None) -> str:
    low, high = profile.target
    longer = min(profile.max_chars, high * 3 // 2)
    tasks = {
        "new": "Задание: пост по статье.",
        "variant": "Задание: другой вариант поста по статье — другая подача и формулировки, те же факты.",
        "shorter": (
            "Задание: сократи предыдущий вариант примерно на треть, оставь главное и ссылку."
            if previous
            else f"Задание: короткий пост по статье, около {low} символов: только главное."
        ),
        "longer": (
            f"Задание: подробный пост — сделай предыдущий вариант длиннее, добавь факты из статьи, "
            f"до {longer} символов."
            if previous
            else f"Задание: подробный пост по статье, до {longer} символов."
        ),
    }
    parts = [tasks[mode], f"Статья:\n{as_json(_payload(article, lang))}"]
    if previous:
        parts.append(f"Предыдущий вариант поста:\n{previous}")
    return "\n\n".join(parts)


def _voice_request(transcript: str, mode: Mode, profile: Profile, previous: str | None) -> str:
    low, high = profile.target
    longer = min(profile.max_chars, high * 3 // 2)
    tasks = {
        "new": "Задание: пост по расшифровке голосового сообщения.",
        "variant": "Задание: другой вариант поста — другая подача и формулировки, те же факты.",
        "shorter": (
            "Задание: сократи предыдущий вариант примерно на треть, оставь главное."
            if previous
            else f"Задание: короткий пост по расшифровке, около {low} символов: только главное."
        ),
        "longer": (
            f"Задание: подробный пост — сделай предыдущий вариант длиннее, только за счёт сказанного "
            f"в расшифровке, до {longer} символов."
            if previous
            else f"Задание: подробный пост по расшифровке, до {longer} символов."
        ),
    }
    parts = [tasks[mode], f"Расшифровка:\n{transcript}"]
    if previous:
        parts.append(f"Предыдущий вариант поста:\n{previous}")
    return "\n\n".join(parts)


def _payload(article: Article, lang: str) -> dict:
    text = article.texts.get(lang) or article.texts["ru"]
    published = article.source.published_at.astimezone(TASHKENT)
    data: dict = {
        "category": article.category,
        "text_language": lang if article.texts.get(lang) else "ru",
        "headline": text.headline,
        "summary": text.summary,
        "details": text.details,
        "source": {
            "name": article.source.name,
            "url": article.source.url,
            "published": published.strftime("%d.%m.%Y %H:%M"),
        },
    }
    if article.doc:
        doc = article.doc
        link = doc.lex.get(lang)
        data["doc"] = {
            "doc_key": doc.doc_key,
            "status": doc.status,
            "lex_url": link.url if link else None,
            "adopted_on": doc.adopted_on.strftime("%d.%m.%Y") if doc.adopted_on else None,
            "effective_on": doc.effective_on.strftime("%d.%m.%Y") if doc.effective_on else None,
        }
    return data


def _fix_request(text: str, flags: list[str]) -> str:
    problems = "\n".join(f"- {flag}" for flag in flags)
    return (
        f"Проверка нашла ошибки в твоём посте:\n{problems}\n\n"
        f"Исправь их и верни пост целиком. Твой пост:\n{text}"
    )


# --- Проверка ---


def _fact_sources(article: Article, lang: str) -> list[tuple[str, str]]:
    """Тексты, откуда в посте могут быть числа и даты: статья на языке поста и на русском, реквизиты."""
    sources = [(_full(text), text_lang) for text_lang, text in article.texts.items() if text]
    published = article.source.published_at.astimezone(TASHKENT)
    facts = [published.strftime("%d.%m.%Y")]
    if article.doc:
        facts.append(article.doc.number)
        facts += [d.strftime("%d.%m.%Y") for d in (article.doc.adopted_on, article.doc.effective_on) if d]
    sources.append((" ".join(facts), "ru"))
    return sources


def _allowed_links(article: Article) -> set[str]:
    urls = {article.source.url, *(ref.url for ref in article.also)}
    if article.doc:
        urls |= {link.url for link in article.doc.lex.values()}
    return urls


def _full(text: Text) -> str:
    return f"{text.headline}\n{text.summary}\n{text.details}"
