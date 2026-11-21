# rag-sync

**Keep your RAG index in sync with your database.** Re-embed only what changed, drop what's gone, and know what every sync cost.

## Why I built this

I built rag-sync for a bigger personal project: a chatbot that answers questions from the documents in my own database. Its search worked great the day I built the index. A month later it didn't. Documents had been edited, replaced or deleted, and the index was still a snapshot of day one. Nothing crashed: the chatbot just kept answering, confidently, from chunks that were no longer true.

The usual fixes didn't convince me. Re-indexing everything every night means paying for every embedding again, every time. Only appending new rows leaves the old versions in the index, competing with the new ones. So I wrote the small tool in between, and today it's what keeps that chatbot's search up to date, every hour.

Each source is a SQL query. rag-sync fingerprints every row, compares it with what's already indexed, embeds only what is new or changed, deletes what disappeared, and logs the run.

- **Pays only for what changed.** A sha256 fingerprint per document. Unchanged documents cost nothing.
- **Deletes what's gone.** A row that leaves the query leaves the index, chunks included, so stale versions don't compete with new ones.
- **Sources are SQL.** Any table, view or join in your Postgres. No connectors, no export step.
- **Per-user visibility.** Return an `owner_id` and each user only finds their own documents plus shared ones. On Supabase this is enforced by row level security ([`sql/supabase.sql`](sql/supabase.sql)).
- **Safe by default.** `--dry-run` shows what would change and what it would cost. A sync that would delete most of a source (a broken query, an empty table) is refused unless you pass `--force`.
- **A log of every run** in `rag_sync.runs`: how much your data actually drifts, and what keeping up with it costs.
- **Just Postgres + pgvector.** Embeddings through any OpenAI-compatible endpoint (Vercel AI Gateway by default).

## What it did for my chatbot

These are the numbers from my own database (Postgres on Supabase, about 460 documents, `text-embedding-3-small`):

| | |
|---|---|
| First full index | 6,878 chunks, 2.9M tokens, **$0.058**, about 10 minutes |
| A sync with nothing to do | 11 seconds, **$0** |
| A typical hourly sync | a handful of changed documents, a fraction of a cent |

The reason I wanted an index at all was to find documents by what's *inside* them, not by their names. Before wiring it into the chatbot I measured it. I took 20 documents, asked for each of them in three different ways, and checked whether the right one came back in the top 3:

| How the question is asked | Keyword search on names | An LLM picking from the list of names | rag-sync |
|---|---|---|---|
| By the document's name | 13/20 | **20/20** | 18/20 |
| By a section heading inside it | 0/16 | 9/16 | **14/16** |
| A question about its content | 2/20 | 10/20 | **16/20** |

When the user names the document, the chatbot's existing tools are enough. When they ask about what's in it, the index is what finds it. So I kept both, and the chatbot has a tool for each. I also checked the per-user isolation with real accounts, searching as each user: nobody ever got someone else's documents.

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

Two things I learned wiring it into my chatbot:

- **Pick a similarity floor from your own data.** In my index, questions whose answer really was in the user's documents always scored 0.44 or more (median 0.53), and unrelated questions never went above 0.36 (median 0.25). My chatbot's tool drops anything under 0.3 and tells the model that under 0.4 the answer is probably not in the documents, so it says so instead of making something up.
- **Group by document.** A long document can fill the top results with its own chunks. Return each document once, with its best passages.

## Running it on a schedule

A sync with nothing to do costs one query and no embeddings, so running it often is cheap. I run it every hour with a systemd timer on a small always-on machine:

```ini
# /etc/systemd/system/rag-sync.service
[Service]
Type=oneshot
User=rag-sync
WorkingDirectory=/opt/rag-sync
EnvironmentFile=/etc/rag-sync/rag-sync.env
ExecStart=/opt/rag-sync/.venv/bin/rag-sync -c /path/to/rag-sync.yaml sync

# /etc/systemd/system/rag-sync.timer
[Timer]
OnCalendar=*:07
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` catches up after the machine was off, and a oneshot service never overlaps with itself. With cron, use `flock` for the same effect:

```cron
7 * * * *  cd /path/to/rag-sync && flock -n /tmp/rag-sync.lock .venv/bin/rag-sync sync >> sync.log 2>&1
```

`rag-sync status` shows the indexed documents per source and the last runs; `rag_sync.runs` has the full history.

## Why there is no vector index

Searches filter by source and owner. An approximate index (HNSW) picks its candidates first and filters afterwards, so a filtered search can return fewer results than you asked for, or none. Exact search over tens of thousands of chunks takes milliseconds (about 150 ms per search on my index, network included). When you outgrow it, [`sql/schema.sql`](sql/schema.sql) has the index and the pgvector setting (iterative scans) to add.

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

`DATABASE_URL` and the API key come from the environment or a `.env` file in the working directory. Only the project's own files are tracked by git: your `.env`, your `rag-sync.yaml` and anything else you keep in the folder stay local.

## Development

```bash
pip install -e ".[dev]"
pytest                                   # unit tests, no network or database

# end-to-end against a throwaway Postgres with pgvector
docker run -d --name rag-sync-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 pgvector/pgvector:pg17
RAG_SYNC_TEST_DATABASE_URL=postgresql://postgres:pg@localhost:55432/postgres pytest
```

## What's next

I'll update this README after the index has run for a month on my project, with what `rag_sync.runs` says about how much the data really drifts and what keeping up with it costs. On the list after that:

- `rag-sync eval`: a fixed set of questions with the document each should find, measured as recall@k after every sync, to track retrieval quality over time and not only freshness.
- Downloading only what changed: hash the content in SQL, so a sync with nothing to do moves a few KB instead of every document's text.
- Detecting documents that replace others without sharing an id ("the 2026 policy supersedes the 2025 one").

## License

MIT
