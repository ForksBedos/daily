"""Проверка поста перед выдачей (docs/plan/stage-7-posts.md, «Проверка»).

Числа и даты поста должны быть в статье (сравниваются значения, как в llm/qa.py), длина — в лимите
площадки, ссылки — только из статьи и есть там, где профиль их требует, язык — тот, что заказан.
Хэштегов не больше, чем разрешает профиль. Пост по голосовому (voice=True) сверяется с расшифровкой:
источника у него нет, поэтому ссылки не требуются, а любая ссылка — выдуманная.
"""

from __future__ import annotations

import re

from uzdigest.extract.normalize import canonical_url
from uzdigest.llm import qa
from uzdigest.posts.profiles import Profile

URL = re.compile(r"https?://[^\s<>()\[\]«»\"']+")
HASHTAG = re.compile(r"(?<![\w#])#(\w+)")
# Площадки обрывают хэштег на первом не-словесном знаке: #ijtimoiyqo‘llab станет #ijtimoiyqo
BROKEN_HASHTAG = re.compile(r"(?<![\w#])(#\w+[‘’ʻʼ'`-]\w+)")
_URL_TAIL = ".,;:!?…*_"  # точка или звёздочки разметки сразу после адреса — не часть адреса
_CYRILLIC = re.compile(r"[а-яёўқғҳ]", re.I)
_LATIN = re.compile(r"[a-z]", re.I)
_APOSTROPHES = re.compile(r"[‘’ʻʼ'`]")
_EN_WORDS = {
    "the", "and", "of", "to", "in", "for", "is", "are", "will", "from", "with", "on", "by", "this", "that",
    "be", "has", "have", "was", "were", "an", "as", "at", "its",
}  # fmt: skip
_UZ_WORDS = {
    "va", "bilan", "uchun", "yil", "yilda", "yildan", "ham", "bu", "esa", "emas", "kerak", "mumkin",
    "bo'yicha", "bo'ladi", "qilindi", "etiladi", "qaror", "soliq", "son", "dan", "ga",
}  # fmt: skip
MIN_LETTERS = 20  # короче — язык не определяется


def count_chars(text: str) -> int:
    """Длина в единицах UTF-16, как считает Telegram: эмодзи — 2. Для других площадок — с запасом."""
    return len(text.encode("utf-16-le")) // 2


def links(text: str) -> list[str]:
    return [url.rstrip(_URL_TAIL) for url in URL.findall(text)]


def hashtags(text: str) -> list[str]:
    return HASHTAG.findall(URL.sub(" ", text))


def detect_lang(text: str) -> str | None:
    """ru, en или uz (латиница) по буквам и частым словам; None — текста слишком мало."""
    clean = HASHTAG.sub(" ", URL.sub(" ", text))
    cyrillic, latin = len(_CYRILLIC.findall(clean)), len(_LATIN.findall(clean))
    if cyrillic + latin < MIN_LETTERS:
        return None
    if cyrillic > latin:
        return "ru"
    words = re.findall(r"[a-z']+", _APOSTROPHES.sub("'", clean.lower()))
    uzbek = sum(w in _UZ_WORDS for w in words) + len(re.findall(r"\b\w*[og]'", " ".join(words)))
    english = sum(w in _EN_WORDS for w in words)
    return "uz" if uzbek > english else "en"


def check(
    text: str,
    lang: str,
    profile: Profile,
    sources: list[tuple[str, str]],
    allowed: set[str],
    advice: str = "",
    *,
    voice: bool = False,
) -> list[str]:
    """Замечания к посту. sources — тексты статьи с их языком; allowed — адреса, которые можно ставить."""
    problems = []
    where = "расшифровке" if voice else "статье"
    length = count_chars(text)
    if length > profile.max_chars:
        problems.append(f"длина {length} больше лимита {profile.max_chars}")

    found, known = qa.extract(URL.sub(" ", text), lang), qa.Facts()
    for source, source_lang in sources:
        known.update(qa.extract(source, source_lang))
    for _, _, year in known.dates:
        if year:
            known.numbers.setdefault(float(year), str(year))  # «в 2026 году» при дате «28.10.2026» в статье
    problems += [f"нет в {where}: {raw}" for raw in found.missing_numbers(known)]
    problems += [f"нет в {where}: {raw}" for d, raw in found.dates.items() if not known.has_date(d)]

    urls = links(text)
    permitted = {canonical_url(url) for url in allowed}
    if profile.link == "none":
        if urls:
            problems.append(f"в посте для {profile.name} ссылок быть не должно: {', '.join(urls)}")
    elif voice:
        if urls:
            problems.append(f"ссылка не из расшифровки: {', '.join(urls)}")
    else:
        foreign = [url for url in urls if canonical_url(url) not in permitted]
        if foreign:
            problems.append(f"ссылка не из статьи: {', '.join(foreign)}")
        if len(foreign) == len(urls):
            problems.append("нет ссылки на источник")

    if advice and advice.casefold() not in text.casefold():
        problems.append(f"нет блока «{advice}»")

    tags = hashtags(text)
    if len(tags) > profile.hashtags[1]:
        problems.append(f"хэштегов {len(tags)}, можно не больше {profile.hashtags[1]}")
    broken = BROKEN_HASHTAG.findall(URL.sub(" ", text))
    if broken:
        problems.append(f"хэштег оборвётся на апострофе или дефисе: {', '.join(broken)}")
    detected = detect_lang(text)
    if detected is not None and detected != lang:
        problems.append(f"пост на языке {detected}, а заказан {lang}")
    return problems
