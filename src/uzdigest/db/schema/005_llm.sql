-- Этап 5 (docs/plan/stage-5-llm.md): решения и тексты Claude.

-- Отбор: оценка каждого материала. Кэш по версии промпта — повторная сборка не тратит лимит.
CREATE TABLE triage (
  item_id INTEGER NOT NULL REFERENCES items(id),
  prompt_version TEXT NOT NULL,   -- triage_v1
  relevant INTEGER NOT NULL,
  category TEXT CHECK (category IN ('legislation', 'taxes', 'accounting', 'finance', 'business', 'macro')),
  importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 5),
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (item_id, prompt_version)
);

-- Отпечаток входа: текст или его русская версия изменились — кэшированный текст устарел
ALTER TABLE summaries ADD COLUMN input_hash TEXT;
