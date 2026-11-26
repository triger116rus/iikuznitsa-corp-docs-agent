-- Схема БД агента поиска ответов по корпоративным регламентам (PostgreSQL 16 + pgvector + pg_trgm).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Документ (регламент) и его карточка.
CREATE TABLE IF NOT EXISTS documents (
    id            serial PRIMARY KEY,
    title         text NOT NULL,
    file_name     text NOT NULL UNIQUE,
    department    text,               -- подразделение-владелец (область действия)
    process       text,               -- процесс: пресейл / проектная деятельность / ...
    status        text NOT NULL DEFAULT 'in_force',   -- in_force / superseded
    superseded_by int REFERENCES documents(id),
    owner_contact text,               -- к кому обращаться по документу
    loaded_at     timestamptz NOT NULL DEFAULT now()
);

-- Фрагмент = пункт регламента (единица поиска и цитирования).
CREATE TABLE IF NOT EXISTS chunks (
    id           serial PRIMARY KEY,
    doc_id       int NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    clause_no    text,                -- «2.3» / «5.1.3.1»
    section      text,                -- «2. Назначение РП»
    text         text NOT NULL,
    tokens       int,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('russian', coalesce(section,'') || ' ' || text)) STORED,
    embedding    vector(1024),
    ord          int NOT NULL         -- порядок в документе
);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin(tsv);
CREATE INDEX IF NOT EXISTS chunks_trgm ON chunks USING gin(text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS chunks_emb ON chunks USING hnsw (embedding vector_cosine_ops);

-- Граф связей. Рёбра пишутся при загрузке (regex) и при дообучении.
--   references    — пункт ссылается на другой пункт/раздел/документ (dst_ref_text — как написано в тексте);
--   mentions_role — пункт упоминает роль (узел role:<имя>), связывает регламенты разных подразделений;
--   explains      — разъяснение службы качества относится к пункту/документу;
--   supersedes    — новая редакция заменяет старую.
-- dst_chunk/dst_doc пусты, если цель в корпусе не найдена (висячая ссылка → пробел в базе).
CREATE TABLE IF NOT EXISTS doc_links (
    id           serial PRIMARY KEY,
    src_doc      int REFERENCES documents(id) ON DELETE CASCADE,
    src_chunk    int REFERENCES chunks(id) ON DELETE CASCADE,
    src_answer   int,                 -- для kind=explains
    kind         text NOT NULL,
    dst_doc      int REFERENCES documents(id) ON DELETE CASCADE,
    dst_chunk    int REFERENCES chunks(id) ON DELETE CASCADE,
    dst_node     text,                -- role:РП, process:Арбитраж расчёта себестоимости
    dst_ref_text text,
    origin       text NOT NULL DEFAULT 'regex',  -- regex / llm / manual
    confidence   real NOT NULL DEFAULT 1.0,
    evidence     text                 -- цитата, из которой извлечено ребро
);
CREATE INDEX IF NOT EXISTS doc_links_src ON doc_links(src_chunk);
CREATE INDEX IF NOT EXISTS doc_links_dst ON doc_links(dst_chunk);
CREATE INDEX IF NOT EXISTS doc_links_node ON doc_links(dst_node);

-- Запросы в службу качества (кейс 1 и 3).
CREATE TABLE IF NOT EXISTS tickets (
    id           serial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    user_name    text,
    department   text,
    question     text NOT NULL,
    context      jsonb,               -- что агент нашёл и почему не ответил
    status       text NOT NULL DEFAULT 'open',   -- open / answered
    answer       text,
    answered_by  text,
    answered_at  timestamptz,
    mail_to      text,
    mail_body    text                 -- текст письма в службу качества
);

-- База разъяснений службы качества (дообучение без переобучения модели).
CREATE TABLE IF NOT EXISTS kb_answers (
    id           serial PRIMARY KEY,
    ticket_id    int REFERENCES tickets(id),
    question     text NOT NULL,
    answer       text NOT NULL,
    doc_id       int REFERENCES documents(id),   -- к какому регламенту привязано
    clause_no    text,
    department   text,
    approved_by  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('russian', question || ' ' || answer)) STORED,
    embedding    vector(1024)
);
CREATE INDEX IF NOT EXISTS kb_answers_tsv ON kb_answers USING gin(tsv);

-- Диалоги (уточняющие вопросы) и журнал запросов (аудит, аналитика, калькулятор).
CREATE TABLE IF NOT EXISTS dialogs (
    id           text PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    state        jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS query_log (
    id           serial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    dialog_id    text,
    user_name    text,
    question     text,
    outcome      text,                -- answered / clarify / escalated / error
    steps        jsonb,
    citations    jsonb,
    answer       text,
    tokens_in    int,
    tokens_out   int,
    llm_calls    int,
    latency_ms   int,
    feedback     text
);
