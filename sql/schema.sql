-- rag-sync: the index tables. Run once per database (psql, or the Supabase SQL editor).
--
-- Everything lives in its own schema, "rag_sync", away from your app tables.
-- The vector size (1536) matches openai/text-embedding-3-small; change it if
-- you use a model with a different size.

create extension if not exists vector;   -- on Supabase it is already in the "extensions" schema

create schema if not exists rag_sync;

-- One row per source document, with the fingerprint of what was embedded.
create table if not exists rag_sync.documents (
    source       text        not null,
    doc_id       text        not null,
    owner_id     text,                       -- null = visible to everyone
    title        text,
    content_hash text        not null check (content_hash ~ '^[0-9a-f]{64}$'),
    chunk_count  integer     not null check (chunk_count >= 0),
    metadata     jsonb       not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
    updated_at   timestamptz,                -- as reported by the source, informative only
    synced_at    timestamptz not null default now(),
    primary key (source, doc_id)
);

create table if not exists rag_sync.chunks (
    source      text    not null,
    doc_id      text    not null,
    chunk_index integer not null check (chunk_index >= 0),
    owner_id    text,                        -- copied from the document, so searches can filter without a join
    content     text    not null,
    embedding   vector(1536) not null,
    primary key (source, doc_id, chunk_index),
    foreign key (source, doc_id) references rag_sync.documents (source, doc_id) on delete cascade
);

-- One row per sync of one source: how much changed, and what it cost.
create table if not exists rag_sync.runs (
    id              bigint generated always as identity primary key,
    source          text        not null,
    started_at      timestamptz not null,
    finished_at     timestamptz not null default now(),
    added           integer     not null,
    modified        integer     not null,
    deleted         integer     not null,
    unchanged       integer     not null,
    chunks_embedded integer     not null,
    tokens          integer     not null,
    cost_usd        numeric(12, 6) not null
);

create index if not exists runs_source_idx on rag_sync.runs (source, finished_at desc);

-- No vector index on purpose. Searches filter by source and owner, and an
-- approximate (HNSW) index applies those filters after picking candidates,
-- so it can return fewer results than asked. Exact search over a few tens of
-- thousands of chunks takes milliseconds. When you outgrow it, add:
--   create index on rag_sync.chunks using hnsw (embedding vector_cosine_ops);
-- and turn on iterative scans (pgvector >= 0.8): set hnsw.iterative_scan = relaxed_order;

-- Row level security on, no policies: nobody but the table owner (the role
-- that runs rag-sync) can read or write. sql/supabase.sql opens read access
-- for logged-in users, limited to their own documents.
alter table rag_sync.documents enable row level security;
alter table rag_sync.chunks    enable row level security;
alter table rag_sync.runs      enable row level security;
