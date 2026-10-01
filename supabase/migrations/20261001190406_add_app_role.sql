-- Phase 7 step 3: the least-privilege role the hosted app connects as, and the
-- rate-limit table. Works on current_schema() like the lockdown migration: public
-- under supabase db reset / db push, a throwaway schema in the live-DB tests.
-- Idempotent. The pinned migrations are not touched.
--
-- kb_app is created NOLOGIN with no password. LOGIN and the password are set by
-- hand on each database (alter role kb_app with login password '...'); neither is
-- ever written to a file.
--
-- statement_timeout is set on the role, not only as a pool startup option, so it
-- holds for every backend the transaction pooler opens for kb_app. It also keeps
-- query text out of auto_explain logs (log_min_duration 10000 ms > 5000 ms).
--
-- The kb_ functions are SECURITY INVOKER, so kb_app needs EXECUTE on the helpers
-- the search functions call (kb_lexical_tsquery, kb_article_v1_sha256) and SELECT
-- on the tables they read. kb_join_tags is used only by the generated column and
-- is not granted.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'kb_app') then
    create role kb_app nologin nosuperuser nocreatedb nocreaterole nobypassrls noinherit;
  end if;
end
$$;

-- Re-asserted on every run; LOGIN and the password are left as set by hand.
-- NOSUPERUSER is set at creation only: a non-superuser may not name the SUPERUSER
-- attribute in ALTER ROLE, and cannot make a role superuser either.
alter role kb_app nocreatedb nocreaterole nobypassrls noinherit;
alter role kb_app set statement_timeout = '5000ms';
alter role kb_app set search_path = public, extensions;

do $$
declare
  s text := current_schema();
  t text;
begin
  if s is null then
    raise exception 'add_app_role: no current schema';
  end if;

  execute format('grant usage on schema %I to kb_app', s);
  execute 'grant usage on schema extensions to kb_app';

  foreach t in array array['articles', 'embedding_configs', 'article_embeddings'] loop
    execute format('grant select on %I.%I to kb_app', s, t);
  end loop;

  execute format('grant execute on function %I.kb_search_lexical(text, int) to kb_app', s);
  execute format('grant execute on function %I.kb_search_semantic(extensions.vector, int, text) '
                 'to kb_app', s);
  execute format('grant execute on function %I.kb_lexical_tsquery(text) to kb_app', s);
  execute format('grant execute on function %I.kb_article_v1_sha256(text, text) to kb_app', s);

  -- One row per (kind, key, window). key is an HMAC (64 hex) of the client IP, or
  -- 'all' for the global daily counter; raw IPs are never stored. The app deletes
  -- windows older than one day.
  execute format($t$
    create table if not exists %I.kb_rate_counters (
      kind         text        not null check (kind in ('search_ip_10m', 'search_ip_day',
                                                       'search_global_day', 'gate_ip_10m')),
      key          text        not null check (key ~ '^[0-9a-f]{64}$' or key = 'all'),
      window_start timestamptz not null,
      count        int         not null check (count >= 1),
      primary key (kind, key, window_start),
      check ((key = 'all') = (kind = 'search_global_day'))
    )$t$, s);
  execute format('create index if not exists kb_rate_counters_window_start_idx '
                 'on %I.kb_rate_counters (window_start)', s);
  execute format('alter table %I.kb_rate_counters enable row level security', s);
  execute format('revoke all on %I.kb_rate_counters from anon, authenticated, public', s);
  execute format('grant select, insert, update, delete on %I.kb_rate_counters to kb_app', s);

  -- D10: RLS stays on everywhere; the only policies are for kb_app.
  foreach t in array array['articles', 'embedding_configs', 'article_embeddings'] loop
    execute format('drop policy if exists kb_app_select on %I.%I', s, t);
    execute format('create policy kb_app_select on %I.%I for select to kb_app using (true)', s, t);
  end loop;
  execute format('drop policy if exists kb_app_all on %I.kb_rate_counters', s);
  execute format('create policy kb_app_all on %I.kb_rate_counters for all to kb_app '
                 'using (true) with check (true)', s);
end
$$;
