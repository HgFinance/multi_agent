begin;

-- 20260801001200 changed evidence_chunks to 1024 dimensions. Keep the
-- service-role RPC signature aligned with the physical column and the
-- ingestion contract; a 1536-vector query must never silently reach it.
drop function if exists api.match_evidence_chunks(
  extensions.vector(1536), timestamptz, text[], integer, double precision
);

create or replace function api.match_evidence_chunks(
  query_embedding extensions.vector(1024),
  query_as_of timestamptz,
  allowed_license_scopes text[],
  match_count integer default 20,
  minimum_similarity double precision default 0.0
)
returns table (
  chunk_id uuid,
  document_version_id uuid,
  content text,
  published_at timestamptz,
  observed_at timestamptz,
  similarity double precision
)
language sql
stable
security definer
set search_path = pg_catalog, research, extensions
as $$
  select
    chunks.chunk_id,
    chunks.document_version_id,
    chunks.content,
    chunks.published_at,
    chunks.observed_at,
    1 - (chunks.embedding <=> query_embedding) as similarity
  from research.evidence_chunks as chunks
  where chunks.embedding is not null
    and chunks.observed_at <= query_as_of
    and (chunks.published_at is null or chunks.published_at <= query_as_of)
    and chunks.license_scope = any(allowed_license_scopes)
    and 1 - (chunks.embedding <=> query_embedding) >= minimum_similarity
  order by chunks.embedding <=> query_embedding
  limit greatest(1, least(match_count, 100));
$$;

revoke execute on function api.match_evidence_chunks(
  extensions.vector(1024), timestamptz, text[], integer, double precision
) from public, anon, authenticated;
grant execute on function api.match_evidence_chunks(
  extensions.vector(1024), timestamptz, text[], integer, double precision
) to service_role;

commit;
