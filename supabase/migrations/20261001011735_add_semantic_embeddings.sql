-- Phase 5 slice 2: pgvector article embeddings and the semantic ranking function.
-- The pinned lexical migration is not touched. Exact cosine search, no vector index.
-- Every pgvector object is schema-qualified (extensions.*) so nothing here depends
-- on search_path. Change only with an approved DECISIONS.md entry.

create extension if not exists vector with schema extensions;

-- One row per embedding configuration. config is the canonical config object;
-- config_sha256 is the sha256 of its canonical bytes, computed outside SQL.
create table embedding_configs (
  config_sha256    text primary key check (config_sha256 ~ '^[0-9a-f]{64}$'),
  config           jsonb not null,
  model            text not null check (btrim(model) <> ''),
  dimensions       int  not null check (dimensions = 1536),
  template_version text not null check (btrim(template_version) <> '')
);

-- One row per (config, article). Keyed on source_article_id, which survives
-- db reset and re-ingest. ON DELETE RESTRICT: pruning an embedded article fails
-- loudly instead of silently shrinking the semantic corpus.
create table article_embeddings (
  config_sha256      text not null
                       references embedding_configs (config_sha256) on delete cascade,
  source_article_id  text not null
                       references articles (source_article_id) on delete restrict,
  source_text_sha256 text not null check (source_text_sha256 ~ '^[0-9a-f]{64}$'),
  input_kind         text not null check (input_kind in ('text', 'tokens')),
  input_sha256       text not null check (input_sha256 ~ '^[0-9a-f]{64}$'),
  input_tokens       int  not null check (input_tokens between 1 and 8192),
  -- Token IDs are sent only for articles cut at the limit.
  truncated          boolean not null check (truncated = (input_kind = 'tokens')),
  -- A zero vector would make cosine distance NaN.
  embedding          extensions.vector(1536) not null
                       check (extensions.vector_norm(embedding) > 0),
  loaded_at          timestamptz not null default now(),
  primary key (config_sha256, source_article_id)
);

-- sha256 (hex) of the article-v1 text "Title: {title}\n\n{fullText}" as UTF-8.
-- Must equal the source_text_sha256 the embedding pipeline computed.
create function kb_article_v1_sha256(title text, full_text text) returns text
  language sql stable parallel safe as $$
  select encode(sha256(convert_to('Title: ' || title || E'\n\n' || full_text, 'UTF8')), 'hex')
$$;

-- Exact cosine ranking over one embedding config. Score = 1 - cosine distance
-- (cosine similarity, not a probability). Ties break on source_article_id with
-- C collation. Fails loudly (fixed messages, custom SQLSTATEs, no values) instead
-- of searching a partial, stale or mismatched set:
--   KBF01 k outside 1..50
--   KBF02 cfg is not a loaded embedding config
--   KBF03 query vector is not 1536-d or is all zeros (argument typmods are
--         ignored by Postgres, so the dimension is checked here)
--   KBF04 an article has no embedding for cfg, or counts differ
--   KBF05 an embedding was made from text that no longer matches its article
create function kb_search_semantic(q extensions.vector, k int, cfg text)
returns table (rank int, source_article_id text, title text, url text,
               category text, score double precision)
  language plpgsql stable as $$
#variable_conflict use_column
begin
  if k is null or k < 1 or k > 50 then
    raise exception 'kb_search_semantic: k out of range' using errcode = 'KBF01';
  end if;
  if cfg is null or not exists (
      select 1 from embedding_configs c where c.config_sha256 = cfg) then
    raise exception 'kb_search_semantic: unknown embedding config' using errcode = 'KBF02';
  end if;
  if q is null or extensions.vector_dims(q) <> 1536 or extensions.vector_norm(q) = 0 then
    raise exception 'kb_search_semantic: invalid query vector' using errcode = 'KBF03';
  end if;
  if exists (
      select 1 from articles a
      where not exists (
        select 1 from article_embeddings e
        where e.config_sha256 = cfg and e.source_article_id = a.source_article_id))
     or (select count(*) from article_embeddings e where e.config_sha256 = cfg)
        <> (select count(*) from articles) then
    raise exception 'kb_search_semantic: embeddings incomplete for config' using errcode = 'KBF04';
  end if;
  if exists (
      select 1 from article_embeddings e
      join articles a on a.source_article_id = e.source_article_id
      where e.config_sha256 = cfg
        and e.source_text_sha256 <> kb_article_v1_sha256(a.title, a.full_text)) then
    raise exception 'kb_search_semantic: embeddings stale for config' using errcode = 'KBF05';
  end if;

  return query
  with s as (
    select a.source_article_id as sid, a.title as t, a.url as u, a.category as c,
           (1 - (e.embedding operator(extensions.<=>) q))::double precision as sc
    from article_embeddings e
    join articles a on a.source_article_id = e.source_article_id
    where e.config_sha256 = cfg
  )
  select (row_number() over (order by s.sc desc, s.sid collate "C"))::int,
         s.sid, s.t, s.u, s.c, s.sc
  from s
  order by 1
  limit k;
end
$$;
