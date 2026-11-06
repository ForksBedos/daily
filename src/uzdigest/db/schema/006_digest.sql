-- Этап 6 (docs/plan/stage-6-render.md): модель дайджеста (то же, что digest.json).
-- По ней rerender пересобирает файлы без сети и Claude, а следующие дайджесты находят акты,
-- которые выходили как «ожидает публикации», для блока «Обновления».
ALTER TABLE digests ADD COLUMN model_json TEXT;
