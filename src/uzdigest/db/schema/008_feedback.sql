-- Этап 8 (docs/plan/stage-8-automation.md): замечания команды к дайджесту и постам.
CREATE TABLE feedback (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  digest_date TEXT,               -- ГГГГ-ММ-ДД; пусто — замечание не к конкретному дню
  article_id TEXT,                -- как в digest.json: 2026-11-03-002
  kind TEXT NOT NULL CHECK (kind IN ('missed', 'duplicate', 'extra', 'error', 'translation', 'post', 'other')),
  text TEXT NOT NULL,
  author TEXT,
  source TEXT NOT NULL CHECK (source IN ('cli', 'telegram'))
);
CREATE INDEX feedback_date ON feedback(digest_date);
