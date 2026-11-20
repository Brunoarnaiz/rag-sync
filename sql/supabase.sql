-- Optional, Supabase only: let logged-in users search the index from your app.
-- Run after schema.sql.
--
-- Each user sees their own documents (owner_id = their auth.uid()) plus the
-- shared ones (owner_id is null). The search function runs as the caller
-- (security invoker), so these policies apply to it as well.

revoke all on schema rag_sync from public, anon, service_role;
revoke all on all tables in schema rag_sync from public, anon, authenticated, service_role;

grant usage on schema rag_sync to authenticated;
grant select on rag_sync.documents, rag_sync.chunks to authenticated;

create policy documents_read on rag_sync.documents
    for select to authenticated
    using (owner_id is null or owner_id = (select auth.uid())::text);

create policy chunks_read on rag_sync.chunks
    for select to authenticated
    using (owner_id is null or owner_id = (select auth.uid())::text);

-- Recommended: a null owner_id means "everyone", so a query that forgets to
-- return owner_id would make private documents public. If only some sources
-- are shared, say so, and a broken query fails instead of leaking:
--   alter table rag_sync.documents add constraint documents_owner_required
--       check (owner_id is not null or source in ('tools'));
--   alter table rag_sync.chunks add constraint chunks_owner_required
--       check (owner_id is not null or source in ('tools'));

-- Call it with supabase.rpc('rag_sync_search', { p_embedding, p_source, p_limit }).
-- The app computes p_embedding with the same model rag-sync uses.
create or replace function public.rag_sync_search(
    p_embedding extensions.vector,
    p_source    text    default null,
    p_limit     integer default 5
)
returns table (source text, doc_id text, title text, content text, similarity double precision)
language sql
stable
security invoker
set search_path = ''
as $$
    select c.source, c.doc_id, d.title, c.content,
           (1 - (c.embedding operator(extensions.<=>) p_embedding))::double precision
    from rag_sync.chunks c
    join rag_sync.documents d on d.source = c.source and d.doc_id = c.doc_id
    where p_source is null or c.source = p_source
    order by c.embedding operator(extensions.<=>) p_embedding
    limit least(greatest(coalesce(p_limit, 5), 1), 20);
$$;

-- Only logged-in users. Not service_role: it bypasses row level security, so
-- with access to the schema it would see everyone's documents.
revoke all on function public.rag_sync_search(extensions.vector, text, integer) from public, anon, service_role;
grant execute on function public.rag_sync_search(extensions.vector, text, integer) to authenticated;

notify pgrst, 'reload schema';
