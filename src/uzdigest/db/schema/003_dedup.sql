-- Этап 3 (docs/plan/stage-3-dedup.md): префильтр, правовые ключи, решения по парам.

-- Рубрики из RSS (<category>), JSON-список: «Спорт», «Погода», «Происшествия» отсекает префильтр
ALTER TABLE items ADD COLUMN categories TEXT;
ALTER TABLE items ADD COLUMN prefilter TEXT CHECK (prefilter IN ('pass', 'filtered'));
ALTER TABLE items ADD COLUMN prefilter_reason TEXT;

ALTER TABLE item_legal_refs ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0;
ALTER TABLE item_legal_refs ADD COLUMN year_guessed INTEGER NOT NULL DEFAULT 0;
CREATE INDEX item_legal_refs_doc_key ON item_legal_refs(doc_key);

-- Решения «один сюжет / разные» по парам материалов, которые важнее правил:
-- llm — Claude по серой зоне (этап 5), manual — разметка команды. Приоритет: manual > llm > rule.
CREATE TABLE pair_decisions (
  item_a INTEGER NOT NULL REFERENCES items(id),
  item_b INTEGER NOT NULL REFERENCES items(id),
  decision TEXT NOT NULL CHECK (decision IN ('same', 'different')),
  source TEXT NOT NULL CHECK (source IN ('rule', 'llm', 'manual')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (item_a, item_b, source),
  CHECK (item_a < item_b)
);
