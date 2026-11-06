-- Этап 7 (docs/plan/stage-7-posts.md): посты для соцсетей по статьям дайджеста.
CREATE TABLE posts (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  digest_date TEXT NOT NULL,      -- ГГГГ-ММ-ДД
  article_id TEXT NOT NULL,       -- как в digest.json: 2026-11-03-002
  platform TEXT NOT NULL,         -- код из config/posts.yaml
  lang TEXT NOT NULL CHECK (lang IN ('ru', 'en', 'uz')),
  mode TEXT NOT NULL CHECK (mode IN ('new', 'shorter', 'longer', 'variant')),
  text TEXT NOT NULL,
  chars INTEGER NOT NULL,         -- длина в единицах UTF-16, как считает Telegram
  hashtags TEXT,                  -- JSON-список
  notes TEXT,                     -- что Claude сократил или опустил
  qa_flags TEXT,                  -- JSON-список; непустой — «⚠️ проверить»
  prompt_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,       -- «тот же пост» с тем же входом берётся отсюда, без Claude
  model TEXT NOT NULL,
  calls INTEGER NOT NULL,         -- вызовов Claude: 1, или 2 с повтором после проверки
  author TEXT,
  path TEXT                       -- копия в outputs/ДАТА/posts/
);
CREATE INDEX posts_article ON posts(digest_date, article_id, platform, lang);
CREATE INDEX posts_created ON posts(created_at);
