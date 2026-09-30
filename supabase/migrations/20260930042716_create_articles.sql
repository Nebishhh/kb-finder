-- Phase 4 slice 1: articles table and the "Postgres FTS baseline" lexical search.
-- Ranking config (weights, english config, OR semantics, ts_rank normalization 0,
-- tie-break) is frozen. Change only with an approved DECISIONS.md entry.

-- array_to_string is STABLE, and generated columns need IMMUTABLE.
-- For text[] the output is deterministic, so this wrapper is safe.
create function kb_join_tags(tags text[]) returns text
  language sql immutable parallel safe
  as $$ select coalesce(array_to_string(tags, ' '), '') $$;

-- articles: one row per public KB article. Mirrors data/parsed/articles.jsonl.
create table articles (
  id                bigint generated always as identity primary key,
  source_article_id text not null unique check (source_article_id ~ '^[1-9][0-9]*$'),
  title             text not null check (btrim(title) <> ''),
  url               text not null check (url ~ '^https://td\.usd\.edu/TDClient/33/Portal/KB/Article/[0-9]+/'),
  category          text,
  tags              text[] not null default '{}',
  description       text,
  published_time    timestamptz,
  modified_time     timestamptz,
  full_text         text not null check (btrim(full_text) <> ''),
  question          text,
  answer            text,
  issue             text,
  environment       text,
  cause             text,
  resolution        text,
  sections          jsonb not null,
  staff_only        jsonb,            -- stored, NOT in search_vector, NOT indexed
  audience_labels   text[] not null default '{}',
  footer            text,
  parser_version    text not null,
  warnings          text[] not null default '{}',
  search_vector     tsvector generated always as (
      setweight(to_tsvector('english', title), 'A')
   || setweight(to_tsvector('english', kb_join_tags(tags)), 'B')
   || setweight(to_tsvector('english', coalesce(description, '')), 'B')
   || setweight(to_tsvector('english', full_text), 'C')
  ) stored
);

create index articles_search_vector_idx on articles using gin (search_vector);

-- Query text -> OR of its distinct english lexemes. NULL when there are none.
create function kb_lexical_tsquery(q text) returns tsquery
  language sql immutable parallel safe as $$
  select case when count(*) = 0 then null
    else string_agg(
           '''' || replace(replace(lex, '\', '\\'), '''', '''''') || '''',
           ' | ' order by lex)::tsquery
  end
  from unnest(tsvector_to_array(to_tsvector('english', coalesce(q, '')))) as lex
$$;

create function kb_search_lexical(q text, k int)
returns table (rank int, source_article_id text, title text, url text,
               category text, score real)
  language sql stable parallel safe as $$
  with t as (select kb_lexical_tsquery(q) as tsq),
  hits as (
    select a.source_article_id, a.title, a.url, a.category,
           ts_rank(a.search_vector, t.tsq) as score
    from articles a, t
    where t.tsq is not null and a.search_vector @@ t.tsq
  )
  select (row_number() over (order by score desc, source_article_id collate "C"))::int,
         source_article_id, title, url, category, score
  from hits
  order by 1
  limit k
$$;
