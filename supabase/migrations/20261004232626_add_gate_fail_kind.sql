-- Phase 7 gate change: a global failed-login counter in kb_rate_counters (kind
-- gate_fail_global_day, key 'all'), so a 4-digit PIN is bounded by a cap of
-- failed logins per UTC day. Works on current_schema() like the earlier Phase 7
-- migrations: public under supabase db reset / db push, a throwaway schema in the
-- live-DB tests. Idempotent. Earlier migrations are not touched.
--
-- Only the two CHECKs that name kinds change; each is dropped by name and re-added
-- with every existing kind and rule kept:
--   kb_rate_counters_kind_check  the allowed kinds, plus gate_fail_global_day
--   kb_rate_counters_check       key 'all' exactly for the two global kinds
-- The key, count and primary key rules are unchanged. No grant changes: kb_app
-- already has SELECT, INSERT, UPDATE and DELETE on the table.

do $$
declare
  s text := current_schema();
begin
  if s is null then
    raise exception 'add_gate_fail_kind: no current schema';
  end if;

  execute format('alter table %I.kb_rate_counters drop constraint if exists '
                 'kb_rate_counters_kind_check', s);
  execute format($c$
    alter table %I.kb_rate_counters add constraint kb_rate_counters_kind_check
      check (kind in ('search_ip_10m', 'search_ip_day', 'search_global_day', 'gate_ip_10m',
                      'gate_fail_global_day'))$c$, s);

  execute format('alter table %I.kb_rate_counters drop constraint if exists '
                 'kb_rate_counters_check', s);
  execute format($c$
    alter table %I.kb_rate_counters add constraint kb_rate_counters_check
      check ((key = 'all') = (kind in ('search_global_day', 'gate_fail_global_day')))$c$, s);
end
$$;
