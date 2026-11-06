-- Схема UzDigest (PLAN.md, раздел 8). Все даты в UTC, ISO 8601.

CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('rss', 'govuz_api', 'html_list', 'lex_rss')),
  url TEXT NOT NULL,
  tier TEXT NOT NULL CHECK (tier IN ('A', 'B', 'C')),
  lang TEXT NOT NULL DEFAULT 'ru',
  params_json TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  etag TEXT,                 -- для условных GET
  last_modified TEXT,
  last_ok_at TEXT,
  last_error TEXT
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('collect', 'digest')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,               -- ok | partial | failed
  window_from TEXT,
  window_to TEXT,
  stats_json TEXT
);

CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  guid TEXT,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  lead TEXT,
  body TEXT,
  lang TEXT NOT NULL,
  published_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id),
  status TEXT NOT NULL DEFAULT 'new'
    CHECK (status IN ('new', 'filtered', 'duplicate', 'primary', 'summarized', 'published')),
  cluster_id INTEGER REFERENCES clusters(id),
  UNIQUE (source_id, guid)
);
CREATE INDEX items_published ON items(published_at);
CREATE INDEX items_cluster ON items(cluster_id);

CREATE TABLE clusters (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('news', 'legal')),
  primary_item_id INTEGER REFERENCES items(id),
  doc_key TEXT,
  first_seen_at TEXT NOT NULL
);
CREATE INDEX clusters_doc_key ON clusters(doc_key);

CREATE TABLE legal_acts (
  id INTEGER PRIMARY KEY,
  doc_key TEXT UNIQUE NOT NULL,
  doc_type TEXT NOT NULL,
  number TEXT NOT NULL,
  issuer TEXT,
  title TEXT,
  adopted_on TEXT,
  effective_on TEXT,
  lex_id INTEGER UNIQUE,
  lex_url TEXT,
  fetched_at TEXT NOT NULL
);

CREATE TABLE item_legal_refs (
  item_id INTEGER NOT NULL REFERENCES items(id),
  doc_key TEXT NOT NULL,
  raw_mention TEXT,
  verify_status TEXT CHECK (verify_status IN ('verified', 'pending', 'draft', 'not_found')),
  checked_at TEXT,
  PRIMARY KEY (item_id, doc_key)
);

CREATE TABLE summaries (
  cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  prompt_version TEXT NOT NULL,
  lang TEXT NOT NULL CHECK (lang IN ('ru', 'en', 'uz')),
  model TEXT NOT NULL,
  category TEXT,
  importance INTEGER,
  headline TEXT,
  summary TEXT,
  details TEXT,
  qa_flags TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (cluster_id, prompt_version, lang)
);

CREATE TABLE digests (
  id INTEGER PRIMARY KEY,
  date TEXT UNIQUE NOT NULL,
  dir TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id),
  created_at TEXT NOT NULL
);

CREATE TABLE digest_entries (
  digest_id INTEGER NOT NULL REFERENCES digests(id),
  cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  position INTEGER NOT NULL,
  PRIMARY KEY (digest_id, cluster_id)
);
