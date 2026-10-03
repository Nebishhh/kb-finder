-- Phase 7 step 2: pin search_path on the five kb_ functions so name resolution in
-- their bodies does not depend on the caller's search_path. Bodies are not touched
-- (ALTER FUNCTION only, no CREATE OR REPLACE), so prosrc and the frozen ranking stay
-- the same. The pinned migrations are not touched. Works on current_schema() like
-- the earlier Phase 7 migrations: public under supabase db reset / db push, a
-- throwaway schema in the live-DB tests.
--
-- The pinned path is the session's existing schemas (current_schemas(false), which
-- drops ones that do not exist, such as "$user"), followed by pg_temp so temporary
-- objects can never shadow anything. Under supabase db reset that is
-- "public, extensions, pg_temp". pg_catalog is still searched first implicitly.
-- The session's search_path is restored at the end (transaction-local), so later
-- statements in the same session are unaffected.

do $$
declare
  s text := current_schema();
  saved text := current_setting('search_path');
  pinned text;
  f text;
begin
  if s is null then
    raise exception 'pin_function_search_path: no current schema';
  end if;

  select string_agg(quote_ident(n), ', ' order by i) into pinned
  from unnest(current_schemas(false)) with ordinality as t(n, i);
  if pinned is null then
    raise exception 'pin_function_search_path: no existing schema on search_path';
  end if;
  perform set_config('search_path', pinned || ', pg_temp', true);

  foreach f in array array['kb_join_tags(text[])',
                           'kb_lexical_tsquery(text)',
                           'kb_search_lexical(text, int)',
                           'kb_article_v1_sha256(text, text)',
                           'kb_search_semantic(extensions.vector, int, text)'] loop
    execute format('alter function %I.%s set search_path from current', s, f);
  end loop;

  perform set_config('search_path', saved, true);
end
$$;
