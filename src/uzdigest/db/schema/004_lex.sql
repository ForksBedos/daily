-- Этап 4 (docs/plan/stage-4-lex.md): сверка упоминаний актов с lex.uz.

-- У каждой языковой версии документа на lex.uz свой id. lex_id — русская версия: у свежих актов
-- Кабмина и ведомств её первые дни нет, на lex.uz только узбекский оригинал.
-- Знак id ничего не значит: у одних актов отрицательный id у латиницы, у других — у кириллицы.
ALTER TABLE legal_acts ADD COLUMN uz_lex_id INTEGER;       -- узбекский, кириллица («Ўзбекча»)
ALTER TABLE legal_acts ADD COLUMN uz_latn_lex_id INTEGER;  -- узбекский, латиница («O'zbekcha»)
ALTER TABLE legal_acts ADD COLUMN en_lex_id INTEGER;       -- перевод на английский

-- Кэш ответов lex.uz: строки выдачи поиска или шапка страницы документа, уже без HTML
CREATE TABLE lex_lookups (
  query TEXT PRIMARY KEY,    -- адрес запроса
  fetched_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
