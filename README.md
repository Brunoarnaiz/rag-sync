# rag-sync

**Keep your RAG index in sync with your database.** Re-embed only what changed, drop what's gone, and know what every sync cost.

A RAG system works great on day one. A month later it starts answering from documents that were edited, replaced or deleted weeks ago. Nothing crashes: the retriever keeps returning the closest chunk it has, and the model answers confidently from stale information. The index is a snapshot, and the data kept moving.

The usual fixes are both bad: re-index everything on a schedule (you pay for every embedding again, every time) or only append new rows (old versions stay in the index and compete with the new ones).

rag-sync is the small tool in between. Each source is a SQL query. rag-sync fingerprints every row, compares it with what is already indexed, embeds only what is new or changed, deletes what disappeared, and logs the run.

- **Pays only for what changed.** A sha256 fingerprint per document. Unchanged documents cost nothing.
- **Deletes what's gone.** A row that leaves the query leaves the index, chunks included, so stale versions don't compete with new ones.
- **Sources are SQL.** Any table, view or join in your Postgres. No connectors, no export step.
- **Per-user visibility.** Return an `owner_id` and each user only finds their own documents plus shared ones. On Supabase this is enforced by row level security ([`sql/supabase.sql`](sql/supabase.sql)).
- **Safe by default.** `--dry-run` shows what would change and what it would cost. A sync that would delete most of a source (a broken query, an empty table) is refused unless you pass `--force`.
- **A log of every run** in `rag_sync.runs`: how much your data actually drifts, and what keeping up with it costs.
- **Just Postgres + pgvector.** Embeddings through any OpenAI-compatible endpoint (Vercel AI Gateway by default).

## Quickstart

You need Postgres with [pgvector](https://github.com/pgvector/pgvector) (Supabase has it) and a key for an embeddings API.

```bash
git clone https://github.com/Brunoarnaiz/rag-sync
cd rag-sync
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env                     # DATABASE_URL and AI_GATEWAY_API_KEY
psql "$DATABASE_URL" -f sql/schema.sql   # once: creates the rag_sync schema
cp rag-sync.example.yaml rag-sync.yaml   # write the queries for your tables

rag-sync sync --dry-run                  # what would change, and the estimated cost
rag-sync sync
rag-sync search "how do I export my data?"
rag-sync status
```

The first sync embeds everything. After that, a sync only touches what changed. Here, after editing one note (the Pro plan went from 29 to 39 EUR), deleting one and adding one:

```
$ rag-sync sync
notes: +1 new  ~1 modified  -1 deleted  =1 unchanged
  embedded 2 chunks, 32 tokens, $0.0000

$ rag-sync sync                      # nothing changed: nothing embedded, nothing paid
notes: up to date  +0 new  ~0 modified  -0 deleted  =3 unchanged

$ rag-sync search "how much is the pro plan?" -k 1
1. 0.429  [notes] Pricing  (1)
   The Pro plan costs 39 EUR per month and includes 10 projects.
```

## How it works

```
 your tables ──SQL query──▶ rows ──fingerprint──▶ compare with rag_sync.documents
                                                     │
                       ┌─────────────────────────────┼──────────────────────────┐
                    new / changed                 unchanged                 missing
                       │                             │                          │
             chunk ▶ embed ▶ replace           nothing to do             delete document
             the document's chunks                                       (chunks cascade)
                       │
                       └──────────────▶ one row in rag_sync.runs per source and sync
```

**The fingerprint** covers the title, the content, the owner, the metadata, the embedding model and the chunk size. Change the model or the chunk size and everything is re-embedded, because old and new vectors are not comparable. A new `updated_at` on the same text is *not* a change: that would be paying for identical embeddings.

**Replacing, not appending.** A changed document has all its old chunks deleted and the new ones inserted in one transaction. A search never sees half of each version.

**Failures are cheap.** Documents are written as soon as their embeddings arrive. If the API fails halfway, the next sync picks up only what's still missing.

## Sources

A source is a name and a SQL query in `rag-sync.yaml`:

```yaml
sources:
  docs: |
    select d.id,
           d.title,
           d.body                                 as content,
           d.user_id                              as owner_id,
           d.updated_at,
           jsonb_build_object('folder', d.folder) as metadata
    from public.documents d
    where d.status = 'published'
```

| column | | |
|---|---|---|
| `id` | required | stable id of the document (any type, stored as text) |
| `title` | required | also prefixed to every chunk before embedding |
| `content` | required | the text to index; split into chunks of `chunk_chars` on paragraph boundaries |
| `owner_id` | optional | who may find it; `null` = everyone |
| `updated_at` | optional | stored for reference only |
| `metadata` | optional | a JSON object stored with the document and returned by searches |

The query decides what is searchable: filter out drafts, disabled or deleted rows right there, and they leave the index on the next sync.

## Searching from your app (Supabase)

[`sql/supabase.sql`](sql/supabase.sql) adds read policies (each user sees `owner_id = auth.uid()` plus shared rows) and a `rag_sync_search` function that runs as the caller, so the policies apply:

```js
const { data } = await supabase.rpc('rag_sync_search', {
  p_embedding: embedding,   // the question, embedded with the same model
  p_source: 'docs',         // or null for all sources
  p_limit: 5,
})
```

From the command line, `rag-sync search "question" --owner <user id>` shows what that user would get.

## Running it on a schedule

A sync with nothing to do costs one query and no embeddings, so running it often is cheap. With cron:

```cron
0 * * * *  cd /path/to/rag-sync && .venv/bin/rag-sync sync >> sync.log 2>&1
```

`rag-sync status` shows the indexed documents per source and the last runs; `rag_sync.runs` has the full history.

## Why there is no vector index

Searches filter by source and owner. An approximate index (HNSW) picks its candidates first and filters afterwards, so a filtered search can return fewer results than you asked for, or none. Exact search over tens of thousands of chunks takes milliseconds. When you outgrow it, [`sql/schema.sql`](sql/schema.sql) has the index and the pgvector setting (iterative scans) to add.

## Configuration

All optional, in `rag-sync.yaml`:

| key | default | |
|---|---|---|
| `model` | `openai/text-embedding-3-small` | must match the vector size in `schema.sql` (1536) |
| `embeddings_url` | `https://ai-gateway.vercel.sh/v1/embeddings` | any OpenAI-compatible endpoint |
| `api_key_env` | `AI_GATEWAY_API_KEY` | environment variable that holds the key |
| `chunk_chars` | `1500` | maximum characters per chunk |
| `batch_size` | `64` | chunks per embeddings request |
| `max_delete_ratio` | `0.5` | refuse syncs that delete more than this share of a source |
| `price_per_million` | `0.02` | USD per million tokens, used only if the API doesn't report the cost |

`DATABASE_URL` and the API key come from the environment or a `.env` file in the working directory. Both are gitignored.

## Development

```bash
pip install -e ".[dev]"
pytest                                   # unit tests, no network or database

# end-to-end against a throwaway Postgres with pgvector
docker run -d --name rag-sync-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 pgvector/pgvector:pg17
RAG_SYNC_TEST_DATABASE_URL=postgresql://postgres:pg@localhost:55432/postgres pytest
```

## Roadmap

- `rag-sync eval`: a fixed set of questions with the document each should find, measured as recall@k after every sync, so you can see retrieval quality over time and not only freshness.
- Detecting documents that replace others without sharing an id ("the 2026 policy supersedes the 2025 one").

## License

MIT
