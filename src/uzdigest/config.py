"""Настройки (config/settings.yaml), реестр источников (config/sources.yaml) и секреты (.env)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"

SourceKind = Literal["rss", "govuz_api", "html_list", "lex_rss"]
Tier = Literal["A", "B", "C"]
BackfillStrategy = Literal["sitemap_daily", "sitemap_monthly", "sitemap", "feed_paged", "list_paged"]


class HttpSettings(BaseModel):
    user_agent: str
    timeout_s: float = 20.0
    retries: int = 3
    min_delay_s: float = 1.0
    max_workers: int = 6


class LlmSettings(BaseModel):
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_calls_per_run: int = 60
    triage_batch: int = 80  # материалов в одном вызове отбора
    summarize_batch: int = 5  # сюжетов в одном вызове текстов
    translate_batch: int = 10  # статей в одном вызове перевода
    max_text_chars: int = 8000  # длиннее — обрезается по абзацу с пометкой «[текст сокращён]»


class DigestSettings(BaseModel):
    max_articles: int = 25  # потолок, а не норма: правовые сюжеты входят всегда, остальные — по важности
    section_limits: dict[str, int] = Field(default_factory=dict)  # раздел → не больше статей (кроме правовых)
    min_importance: int = 3  # неправовой сюжет с меньшей важностью в дайджест не идёт
    min_window_h: int = 24  # окно от конца прошлого дайджеста, но не короче
    max_window_h: int = 96  # и не длиннее: после долгого простоя старое не догоняем


class PostsSettings(BaseModel):
    max_per_day: int = 40  # постов в сутки по Ташкенту: защита подписки Claude от случайного расхода


class BackupSettings(BaseModel):
    keep_last: int | None = None  # сколько дневных копий хранить; None — старые не удаляются (вопрос 4)


class ScheduleSettings(BaseModel):
    """Расписание Планировщика (scripts/install_tasks.ps1 берёт те же значения): только будни."""

    digest_at: str = "09:30"
    collect_from: str = "08:00"
    collect_to: str = "20:00"
    collect_every_h: int = 2

    def collect_slots(self) -> list[str]:
        """Времена сбора за день, ЧЧ:ММ."""
        start, end = time.fromisoformat(self.collect_from), time.fromisoformat(self.collect_to)
        return [
            f"{hour:02d}:{start.minute:02d}" for hour in range(start.hour, end.hour + 1, self.collect_every_h)
        ]


class VoiceSettings(BaseModel):
    dir: Path = Path("data/voice")  # папка обмена с посредником, общая для контейнера и Windows
    max_seconds: int = 300  # голосовое длиннее бот не принимает: минута расшифровывается около 2,5 минуты


class BotUser(BaseModel):
    telegram_id: int
    name: str
    role: Literal["admin", "smm"]


class BotSettings(BaseModel):
    users: list[BotUser] = Field(default_factory=list)  # белый список; остальным — «Доступ запрещен»

    def user(self, telegram_id: int | None) -> BotUser | None:
        return next((u for u in self.users if u.telegram_id == telegram_id), None)

    def admins(self) -> list[BotUser]:
        return [u for u in self.users if u.role == "admin"]


class CollectSettings(BaseModel):
    lookback_h: int = 96  # потолок окна: глубже не листаем API и не догружаем


class EnrichSettings(BaseModel):
    max_pages_per_run: int = 300
    max_attempts: int = 3  # неудачная страница повторяется в следующих запусках, но не больше N раз
    min_text_chars: int = 100


class BackfillSettings(BaseModel):
    max_urls: int = 300  # новых адресов на источник за запуск
    max_pages: int = 5  # для feed_paged и list_paged


class RatesSettings(BaseModel):
    url: str
    currencies: list[str] = Field(default_factory=lambda: ["USD", "EUR", "RUB"])


class DedupSettings(BaseModel):
    """Веса и пороги попарной оценки (dedup/similarity.py). Подбираются на размеченной выборке."""

    pair_window_h: int = 72  # пары сравниваются, только если материалы вышли не дальше N часов друг от друга
    w_title: float = 0.4
    w_anchors: float = 0.25
    w_text: float = 0.35
    anchors_cap: int = 3  # больше N общих якорей оценку не повышают
    merge_threshold: float = 0.45  # не ниже — один сюжет
    text_gray_threshold: float = 0.40  # совпадение всего текста по редким словам: пара идёт к Claude
    repeat_text_threshold: float = (
        0.60  # то же с прошлым выпуском: повтор без вопроса Claude (ответ «да» в 96 %)
    )
    gray_threshold: float = 0.22  # от него до merge_threshold — серая зона, решает Claude (этап 5)
    repeat_days: int = 14  # с дайджестами за столько дней сверяются повторы


class LexSettings(BaseModel):
    """Сверка упоминаний актов с lex.uz (legal/verify.py)."""

    cache_h: float = 12  # ответ lex.uz свежий столько часов; меньше суток — ежедневный запуск ищет заново
    pending_days: int = 7  # столько дней после публикации материала акт ищется; потом — not_found
    guessed_year_days: int = 60  # год угадан (даты рядом нет): верим, если акт принят не раньше N дней


class Settings(BaseModel):
    timezone: str
    db_path: Path
    outputs_dir: Path
    logs_dir: Path
    http: HttpSettings
    llm: LlmSettings
    collect: CollectSettings = Field(default_factory=CollectSettings)
    enrich: EnrichSettings = Field(default_factory=EnrichSettings)
    backfill: BackfillSettings = Field(default_factory=BackfillSettings)
    rates: RatesSettings | None = None
    dedup: DedupSettings = Field(default_factory=DedupSettings)
    lex: LexSettings = Field(default_factory=LexSettings)
    digest: DigestSettings = Field(default_factory=DigestSettings)
    posts: PostsSettings = Field(default_factory=PostsSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    notify: list[str] = Field(default_factory=lambda: ["log"])  # уведомители после каждого run
    bot: BotSettings = Field(default_factory=BotSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)


class GovuzParams(BaseModel):
    authority: str  # код ведомства на gov.uz: soliq, imv, …
    language: str | None = None  # ru | uz; по умолчанию — lang источника
    max_pages: int = 3


class HtmlListParams(BaseModel):
    """Страница-список со ссылками на статьи (html_list и догрузка list_paged)."""

    list_urls: list[str] = Field(default_factory=list)  # по умолчанию — url источника
    link_pattern: str  # регулярное выражение для href статьи
    date_pattern: str | None = None  # дата рядом со ссылкой, первая группа
    date_format: str | None = None  # формат strptime для date_pattern
    pages: str | None = None  # пагинация, например "?PAGEN_1={n}"
    max_pages: int = 1


class Backfill(BaseModel):
    """Догрузка материалов, которые выпали из короткой ленты (PLAN.md, раздел 3, стадия 1)."""

    strategy: BackfillStrategy
    url: str | None = None  # шаблон: {Y} {m} {d} без нулей, {MM} {DD} с нулями, {n} — номер страницы
    url_filter: str | None = None  # подстрока, которая должна быть в адресе статьи
    lastmod_is_published: bool = False  # lastmod в sitemap — время публикации (у uza), а не правки (у kun)
    html: HtmlListParams | None = None  # для list_paged

    @model_validator(mode="after")
    def _check(self) -> Backfill:
        if self.strategy == "list_paged" and self.html is None:
            raise ValueError("для list_paged нужен блок html")
        if self.strategy != "list_paged" and not self.url:
            raise ValueError(f"для {self.strategy} нужен url")
        return self


class Source(BaseModel):
    code: str
    name: str
    kind: SourceKind
    url: str
    tier: Tier
    lang: str = "ru"
    enabled: bool = True
    robots_exempt: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    backfill: Backfill | None = None
    fulltext: bool = True  # false — страницы статей не скачиваются (текст только из ленты/API)
    text_drop_lines: list[str] = Field(default_factory=list)  # регулярные выражения: строка удаляется
    text_stop_lines: list[str] = Field(default_factory=list)  # с этой строки и до конца — не статья
    exclude_url_patterns: list[str] = Field(default_factory=list)  # стоп-рубрики в адресе (префильтр)

    @model_validator(mode="after")
    def _check_params(self) -> Source:
        # Ошибка в реестре видна при загрузке, а не посреди сбора
        if self.kind == "govuz_api":
            GovuzParams.model_validate(self.params)
        elif self.kind == "html_list":
            HtmlListParams.model_validate(self.params)
        return self


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings(path: Path = CONFIG_DIR / "settings.yaml") -> Settings:
    # .env не перезаписывает переменные, уже заданные в окружении
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    settings.db_path = _resolve(settings.db_path)
    settings.outputs_dir = _resolve(settings.outputs_dir)
    settings.logs_dir = _resolve(settings.logs_dir)
    settings.voice.dir = _resolve(settings.voice.dir)
    return settings


def load_sources(path: Path = CONFIG_DIR / "sources.yaml") -> list[Source]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    sources = [Source.model_validate(item) for item in data["sources"]]
    codes = [s.code for s in sources]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        raise ValueError(f"Повторяющиеся коды источников в {path.name}: {duplicates}")
    return sources
