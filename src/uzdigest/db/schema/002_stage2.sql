-- Этап 2 (docs/plan/stage-2-sources.md, задача 2.1): полный текст, точность даты, курсы ЦБ.
--
-- Материалы из HTML-списков и sitemap приходят без заголовка: до стадии enrich
-- items.title = '' (колонка NOT NULL, менять её — значит пересобирать таблицу).

-- feed — текст пришёл с лентой/API, fetched — скачан со страницы, failed — не получилось,
-- skipped — у источника страницы не скачиваются (например, gov.uz отдаёт текст только через API)
ALTER TABLE items ADD COLUMN text_status TEXT
  CHECK (text_status IN ('feed', 'fetched', 'failed', 'skipped'));
ALTER TABLE items ADD COLUMN text_error TEXT;
ALTER TABLE items ADD COLUMN text_attempts INTEGER NOT NULL DEFAULT 0;
-- day — источник знает только дату: published_at = момент обнаружения, но не позже конца того дня
ALTER TABLE items ADD COLUMN published_precision TEXT NOT NULL DEFAULT 'time'
  CHECK (published_precision IN ('time', 'day'));

UPDATE items SET text_status = 'feed' WHERE body IS NOT NULL;
CREATE INDEX items_text_status ON items(text_status);

ALTER TABLE legal_acts ADD COLUMN source TEXT
  CHECK (source IN ('lex_rss', 'lex_search', 'lex_page'));

CREATE TABLE fx_rates (
  date TEXT NOT NULL,        -- дата курса ЦБ, ГГГГ-ММ-ДД
  ccy TEXT NOT NULL,         -- код ISO 4217: USD, EUR, RUB
  nominal INTEGER NOT NULL DEFAULT 1,
  rate REAL NOT NULL,        -- сумов за nominal единиц валюты
  diff REAL,                 -- изменение к предыдущему курсу
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (date, ccy)
);
