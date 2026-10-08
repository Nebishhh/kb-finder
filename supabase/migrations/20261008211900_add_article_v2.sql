-- article-v2 experiment (DECISIONS.md, 2026-10-07): staff_only.text becomes searchable,
-- never displayed, as a versioned configuration beside v1. Adds objects only: no existing
-- column or function is changed or redefined, so v1 lexical and semantic results stay the
-- same. Works on current_schema() like the Phase 7 migrations: public under supabase db
-- reset / migration up, a throwaway schema in the live-DB tests. Not idempotent (plain
-- create and add column, like the pinned migrations).
--
-- Adds:
--   search_vector_v2      the v1 search_vector expression plus staff text at weight D
--   kb_search_lexical_v2  kb_search_lexical over search_vector_v2
--   kb_article_v2_sha256  sha256 of the article-v2 embedding text
--   kb_search_semantic_v2 kb_search_semantic for article-v2 configs, checked against
--                         kb_article_v2_sha256
-- Like every kb_ function these are SECURITY INVOKER with search_path pinned; EXECUTE is
-- revoked from PUBLIC, anon and authenticated and granted to kb_app.

alter table articles add column search_vector_v2 tsvector generated always as (
    setweight(to_tsvector('english', title), 'A')
 || setweight(to_tsvector('english', kb_join_tags(tags)), 'B')
 || setweight(to_tsvector('english', coalesce(description, '')), 'B')
 || setweight(to_tsvector('english', full_text), 'C')
 || setweight(to_tsvector('english', coalesce(staff_only->>'text', '')), 'D')
) stored;

create index articles_search_vector_v2_idx on articles using gin (search_vector_v2);

-- Same query handling, ranking and tie-break as kb_search_lexical; only the vector differs.
create function kb_search_lexical_v2(q text, k int)
returns table (rank int, source_article_id text, title text, url text,
               category text, score real)
  language sql stable parallel safe as $$
  with t as (select kb_lexical_tsquery(q) as tsq),
  hits as (
    select a.source_article_id, a.title, a.url, a.category,
           ts_rank(a.search_vector_v2, t.tsq) as score
    from articles a, t
    where t.tsq is not null and a.search_vector_v2 @@ t.tsq
  )
  select (row_number() over (order by score desc, source_article_id collate "C"))::int,
         source_article_id, title, url, category, score
  from hits
  order by 1
  limit k
$$;

-- sha256 (hex) of the article-v2 text as UTF-8. The embedding pipeline (Part 2) must build
-- the identical bytes. The text is the article-v1 text "Title: {title}\n\n{fullText}", then
-- "\n\nStaff only:\n" followed by staff_only->>'text', only when that text has content:
--   - staff_only SQL NULL, JSON null, no "text" key, or "text" JSON null: no suffix, so the
--     bytes equal the article-v1 text (and kb_article_v1_sha256).
--   - "text" empty, or made only of U+0020 space, U+0009 tab, U+000A LF, U+000B VT, U+000C FF
--     and U+000D CR: no suffix. Python equivalent of this test:
--     text.strip(" \t\n\x0b\x0c\r") == "" (strip with exactly those six characters, never a
--     bare strip()).
--   - Otherwise the suffix carries the text untrimmed, exactly as stored.
--   - Any other character, including non-ASCII whitespace such as U+00A0 or U+2003, is
--     content. Non-ASCII text is encoded as UTF-8 with no Unicode normalization (Python:
--     .encode("utf-8")). Separators are LF only.
create function kb_article_v2_sha256(title text, full_text text, staff_only jsonb) returns text
  language sql stable parallel safe as $$
  select encode(sha256(convert_to(
    'Title: ' || title || E'\n\n' || full_text ||
    case when btrim(coalesce(staff_only->>'text', ''), E' \t\n\x0b\f\r') <> ''
         then E'\n\nStaff only:\n' || (staff_only->>'text')
         else '' end,
    'UTF8')), 'hex')
$$;

-- kb_search_semantic for article-v2: same checks, ranking and tie-break, with two
-- differences. KBF02 also fires when cfg's template_version is not exactly 'article-v2',
-- so a v1 config can never be searched here. KBF05 compares against kb_article_v2_sha256.
--   KBF01 k outside 1..50
--   KBF02 cfg is not a loaded embedding config with template_version 'article-v2'
--   KBF03 query vector is not 1536-d or is all zeros
--   KBF04 an article has no embedding for cfg, or counts differ
--   KBF05 an embedding was made from text that no longer matches its article
create function kb_search_semantic_v2(q extensions.vector, k int, cfg text)
returns table (rank int, source_article_id text, title text, url text,
               category text, score double precision)
  language plpgsql stable as $$
#variable_conflict use_column
begin
  if k is null or k < 1 or k > 50 then
    raise exception 'kb_search_semantic_v2: k out of range' using errcode = 'KBF01';
  end if;
  if cfg is null or not exists (
      select 1 from embedding_configs c
      where c.config_sha256 = cfg and c.template_version = 'article-v2') then
    raise exception 'kb_search_semantic_v2: unknown embedding config' using errcode = 'KBF02';
  end if;
  if q is null or extensions.vector_dims(q) <> 1536 or extensions.vector_norm(q) = 0 then
    raise exception 'kb_search_semantic_v2: invalid query vector' using errcode = 'KBF03';
  end if;
  if exists (
      select 1 from articles a
      where not exists (
        select 1 from article_embeddings e
        where e.config_sha256 = cfg and e.source_article_id = a.source_article_id))
     or (select count(*) from article_embeddings e where e.config_sha256 = cfg)
        <> (select count(*) from articles) then
    raise exception 'kb_search_semantic_v2: embeddings incomplete for config' using errcode = 'KBF04';
  end if;
  if exists (
      select 1 from article_embeddings e
      join articles a on a.source_article_id = e.source_article_id
      where e.config_sha256 = cfg
        and e.source_text_sha256 <> kb_article_v2_sha256(a.title, a.full_text, a.staff_only)) then
    raise exception 'kb_search_semantic_v2: embeddings stale for config' using errcode = 'KBF05';
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

-- search_path pinned exactly as *_pin_function_search_path.sql pins the v1 functions
-- (the session's existing schemas, then pg_temp; restored at the end), then the grants.
do $$
declare
  s text := current_schema();
  saved text := current_setting('search_path');
  pinned text;
  f text;
begin
  if s is null then
    raise exception 'add_article_v2: no current schema';
  end if;

  select string_agg(quote_ident(n), ', ' order by i) into pinned
  from unnest(current_schemas(false)) with ordinality as t(n, i);
  if pinned is null then
    raise exception 'add_article_v2: no existing schema on search_path';
  end if;
  perform set_config('search_path', pinned || ', pg_temp', true);

  foreach f in array array['kb_search_lexical_v2(text, int)',
                           'kb_article_v2_sha256(text, text, jsonb)',
                           'kb_search_semantic_v2(extensions.vector, int, text)'] loop
    execute format('alter function %I.%s set search_path from current', s, f);
    execute format('revoke execute on function %I.%s from public, anon, authenticated', s, f);
    execute format('grant execute on function %I.%s to kb_app', s, f);
  end loop;

  perform set_config('search_path', saved, true);
end
$$;
