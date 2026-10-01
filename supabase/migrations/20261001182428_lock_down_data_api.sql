-- Phase 7 step 1: close the Data API path to the KB objects before hosting.
-- The pinned migrations are not touched. Works on current_schema(): public under
-- supabase db reset / db push, a throwaway schema in the live-DB tests.
--
-- - anon, authenticated and PUBLIC lose every privilege on every table, view,
--   sequence and routine in the schema (all kb_ functions lose PUBLIC EXECUTE).
-- - postgres's default privileges stop granting them rights on future tables,
--   sequences and routines in the schema, and (globally, since per-schema
--   defaults can only add to the global ones) future routines postgres creates
--   no longer get PUBLIC EXECUTE.
-- - RLS is enabled on every table with no policies. The owner (postgres) is not
--   affected, so ingest_kb.py, embed_kb.py load and the app keep working.
-- service_role is left as Supabase sets it.

do $$
declare
  s text := current_schema();
  t record;
begin
  if s is null then
    raise exception 'lock_down_data_api: no current schema';
  end if;

  execute format('revoke all on all tables in schema %I from anon, authenticated, public', s);
  execute format('revoke all on all sequences in schema %I from anon, authenticated, public', s);
  execute format('revoke all on all routines in schema %I from anon, authenticated, public', s);

  execute format('alter default privileges for role postgres in schema %I '
                 'revoke all on tables from anon, authenticated, public', s);
  execute format('alter default privileges for role postgres in schema %I '
                 'revoke all on sequences from anon, authenticated, public', s);
  execute format('alter default privileges for role postgres in schema %I '
                 'revoke all on routines from anon, authenticated, public', s);

  for t in
    select c.relname from pg_class c
    where c.relnamespace = s::regnamespace and c.relkind in ('r', 'p')
    order by c.relname
  loop
    execute format('alter table %I.%I enable row level security', s, t.relname);
  end loop;
end
$$;

alter default privileges for role postgres revoke execute on routines from public;
