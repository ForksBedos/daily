-- Показатели (uzdigest metrics): сколько токенов и времени Claude ушло на пост. Раньше считались только вызовы.
-- У постов, сделанных до этого файла, обе колонки пустые: их вызовы посчитаны, токены неизвестны.
ALTER TABLE posts ADD COLUMN usage_json TEXT;     -- токены по видам, как в LlmBudget.usage
ALTER TABLE posts ADD COLUMN duration_ms INTEGER;
