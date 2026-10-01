"""Catalog tests for the Data API lockdown migration (*_lock_down_data_api.sql).

Offline: the migration exists once and sorts after the pinned migrations.

Live (skipped unless KB_FINDER_TEST_DATABASE_URL is set):
- PublicCatalogTests reads only catalogs of the database as migrated (supabase db reset
  or supabase migration up --local). anon, authenticated and PUBLIC must hold no
  privilege on any table, view, sequence, column or function in public, postgres's
  default privileges must not grant them rights on future objects, RLS must be on
  for every table and no policy may target them. This keeps a later migration from
  reopening the Data API through Supabase's default ACLs.
  Not checked: supabase_admin's default ACLs in public, which postgres cannot alter;
  the object checks above catch anything created under them.
- LockdownMigrationTests runs the migration in a throwaway schema (one transaction,
  always rolled back) after granting Supabase-style defaults there, so it proves the
  migration itself and works on current_schema(), not only on public.
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import test_search_sql as base  # noqa: E402  (helpers only; its TestCase is not imported)

URL = os.environ.get("KB_FINDER_TEST_DATABASE_URL")
MIGRATION_DIR = ROOT / "supabase" / "migrations"
ARTICLES_MIGRATION = MIGRATION_DIR / "20260930042716_create_articles.sql"
SEMANTIC_MIGRATION = MIGRATION_DIR / "20261001011735_add_semantic_embeddings.sql"
LOCKDOWN_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_lock_down_data_api.sql"))

CLOSED_ROLES = ("anon", "authenticated")  # plus PUBLIC (grantee 0)
TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
COLUMN_PRIVS = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
SEQUENCE_PRIVS = ("USAGE", "SELECT", "UPDATE")

if URL:
    import psycopg
    from psycopg import sql

# Grantee filter shared by the ACL queries: PUBLIC or one of CLOSED_ROLES.
OPEN_GRANTEE = "(a.grantee = 0 or pg_get_userbyid(a.grantee) = any(%(roles)s))"
GRANTEE_NAME = "case when a.grantee = 0 then 'PUBLIC' else pg_get_userbyid(a.grantee) end"

# Explicit and implicit (NULL acl means the built-in default) ACL entries.
RELATION_ACL_SQL = f"""
select c.relname, c.relkind::text, {GRANTEE_NAME}, a.privilege_type
from pg_class c
cross join lateral aclexplode(coalesce(
  c.relacl, acldefault(case c.relkind when 'S' then 's'::"char" else 'r'::"char" end, c.relowner))) a
where c.relnamespace = %(ns)s::regnamespace
  and c.relkind in ('r', 'p', 'v', 'm', 'f', 'S')
  and {OPEN_GRANTEE}
order by 1, 3, 4
"""

COLUMN_ACL_SQL = f"""
select c.relname, att.attname, {GRANTEE_NAME}, a.privilege_type
from pg_attribute att
join pg_class c on c.oid = att.attrelid
cross join lateral aclexplode(att.attacl) a
where c.relnamespace = %(ns)s::regnamespace
  and att.attnum > 0 and not att.attisdropped and att.attacl is not null
  and {OPEN_GRANTEE}
order by 1, 2, 3, 4
"""

FUNCTION_ACL_SQL = f"""
select p.oid::regprocedure::text, {GRANTEE_NAME}, a.privilege_type
from pg_proc p
cross join lateral aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
where p.pronamespace = %(ns)s::regnamespace
  and {OPEN_GRANTEE}
order by 1, 2, 3
"""

# Effective privileges, including any inherited through role membership.
EFFECTIVE_SQL = """
select r, c.relname, p from unnest(%(roles)s::text[]) r, pg_class c, unnest(%(table_privs)s::text[]) p
where c.relnamespace = %(ns)s::regnamespace and c.relkind in ('r', 'p', 'v', 'm', 'f')
  and has_table_privilege(r, c.oid, p)
union all
select r, c.relname, 'any column ' || p
from unnest(%(roles)s::text[]) r, pg_class c, unnest(%(column_privs)s::text[]) p
where c.relnamespace = %(ns)s::regnamespace and c.relkind in ('r', 'p', 'v', 'm', 'f')
  and has_any_column_privilege(r, c.oid, p)
union all
select r, c.relname, p from unnest(%(roles)s::text[]) r, pg_class c, unnest(%(sequence_privs)s::text[]) p
where c.relnamespace = %(ns)s::regnamespace and c.relkind = 'S'
  and has_sequence_privilege(r, c.oid, p)
union all
select r, f.oid::regprocedure::text, 'EXECUTE' from unnest(%(roles)s::text[]) r, pg_proc f
where f.pronamespace = %(ns)s::regnamespace and has_function_privilege(r, f.oid, 'EXECUTE')
order by 1, 2, 3
"""

# postgres's defaults for future objects: entries for the schema, plus the global
# defaults (falling back to the built-in default, which gives PUBLIC EXECUTE on
# functions, when there is no global entry).
DEFAULT_ACL_SQL = f"""
with defaults as (
  select 'schema' as scope, d.defaclobjtype::text as objtype, d.defaclacl as acl
  from pg_default_acl d
  where d.defaclrole = 'postgres'::regrole and d.defaclnamespace = %(ns)s::regnamespace
  union all
  select 'global', t.objtype, coalesce(
    (select d.defaclacl from pg_default_acl d
     where d.defaclrole = 'postgres'::regrole and d.defaclnamespace = 0
       and d.defaclobjtype = t.objtype::"char"),
    acldefault(t.aclkind::"char", 'postgres'::regrole))
  -- pg_default_acl codes vs acldefault() codes (sequences: 'S' vs 's').
  from (values ('r', 'r'), ('S', 's'), ('f', 'f')) as t(objtype, aclkind)
)
select d.scope, d.objtype, {GRANTEE_NAME}, a.privilege_type
from defaults d cross join lateral aclexplode(d.acl) a
where {OPEN_GRANTEE}
order by 1, 2, 3, 4
"""

RLS_OFF_SQL = """
select c.relname from pg_class c
where c.relnamespace = %(ns)s::regnamespace and c.relkind in ('r', 'p') and not c.relrowsecurity
order by 1
"""

OPEN_POLICY_SQL = """
select c.relname, pol.polname from pg_policy pol
join pg_class c on c.oid = pol.polrelid
where c.relnamespace = %(ns)s::regnamespace
  and (0 = any(pol.polroles) or exists (
    select 1 from unnest(pol.polroles) r where pg_get_userbyid(r) = any(%(roles)s)))
order by 1, 2
"""


def params(ns):
    return {"ns": ns, "roles": list(CLOSED_ROLES), "table_privs": list(TABLE_PRIVS),
            "column_privs": list(COLUMN_PRIVS), "sequence_privs": list(SEQUENCE_PRIVS)}


def open_access(cur, ns):
    """Every way anon, authenticated or PUBLIC can reach objects in ns. Empty when closed."""
    found = {}
    for name, query in (("relations", RELATION_ACL_SQL), ("columns", COLUMN_ACL_SQL),
                        ("functions", FUNCTION_ACL_SQL), ("effective", EFFECTIVE_SQL),
                        ("default privileges", DEFAULT_ACL_SQL), ("rls off", RLS_OFF_SQL),
                        ("policies", OPEN_POLICY_SQL)):
        cur.execute(query, params(ns))
        found[name] = cur.fetchall()
    return found


EMPTY = {name: [] for name in ("relations", "columns", "functions", "effective",
                               "default privileges", "rls off", "policies")}


class LockdownMigrationFileTest(unittest.TestCase):
    """Offline."""

    def test_one_lockdown_migration_after_the_pinned_ones(self):
        self.assertEqual(len(LOCKDOWN_MIGRATIONS), 1, LOCKDOWN_MIGRATIONS)
        self.assertGreater(LOCKDOWN_MIGRATIONS[0].name, SEMANTIC_MIGRATION.name)
        self.assertGreater(SEMANTIC_MIGRATION.name, ARTICLES_MIGRATION.name)


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class PublicCatalogTests(unittest.TestCase):
    """Catalog reads only, in a read-only transaction. Fails until the lockdown
    migration is applied (supabase db reset or supabase migration up --local)."""

    def setUp(self):
        self.conn = psycopg.connect(URL)
        self.conn.read_only = True
        self.addCleanup(self.conn.close)
        self.cur = self.conn.cursor()

    def test_public_is_closed_to_anon_authenticated_and_public(self):
        self.assertEqual(open_access(self.cur, "public"), EMPTY)

    def test_public_holds_the_kb_objects(self):
        # Guards against the checks above passing vacuously on an empty schema.
        self.cur.execute("select c.relname from pg_class c where c.relnamespace = 'public'::regnamespace "
                         "and c.relkind = 'r' order by 1")
        tables = [r[0] for r in self.cur.fetchall()]
        for name in ("article_embeddings", "articles", "embedding_configs"):
            self.assertIn(name, tables)
        self.cur.execute("select count(*) from pg_proc where pronamespace = 'public'::regnamespace "
                         "and proname like 'kb\\_%'")
        self.assertEqual(self.cur.fetchone()[0], 5)


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class LockdownMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert len(LOCKDOWN_MIGRATIONS) == 1, LOCKDOWN_MIGRATIONS
        cls.migrations = [ARTICLES_MIGRATION.read_text(encoding="utf-8"),
                          SEMANTIC_MIGRATION.read_text(encoding="utf-8")]
        cls.lockdown = LOCKDOWN_MIGRATIONS[0].read_text(encoding="utf-8")
        with psycopg.connect(URL, autocommit=True) as c:
            cls.public_before = (base.public_snapshot(c), open_access(c.cursor(), "public"))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(URL, autocommit=True) as c:
            after = (base.public_snapshot(c), open_access(c.cursor(), "public"))
        assert after == cls.public_before, "public objects or privileges changed"

    def setUp(self):
        self.schema = f"kbtest_{uuid.uuid4().hex}"
        self.conn = psycopg.connect(URL)  # autocommit off: one transaction
        self.addCleanup(self.rollback_and_verify)
        cur = self.cur = self.conn.cursor()
        cur.execute("select exists (select 1 from pg_namespace where nspname = %s)", [self.schema])
        if cur.fetchone()[0]:
            self.fail(f"schema {self.schema} already exists; refusing to run")
        cur.execute(sql.SQL("create schema {}").format(sql.Identifier(self.schema)))
        cur.execute(sql.SQL("set local search_path to {}").format(sql.Identifier(self.schema)))
        for migration in self.migrations:
            cur.execute(migration)
        # What Supabase gives public by default, applied to the throwaway schema.
        ident = sql.Identifier(self.schema)
        for stmt in ("grant all on all tables in schema {} to anon, authenticated, public",
                     "grant all on all sequences in schema {} to anon, authenticated, public",
                     "grant all on all routines in schema {} to anon, authenticated, public",
                     "alter default privileges for role postgres in schema {} "
                     "grant all on tables to anon, authenticated",
                     "alter default privileges for role postgres in schema {} "
                     "grant all on sequences to anon, authenticated",
                     "alter default privileges for role postgres in schema {} "
                     "grant all on routines to anon, authenticated"):
            cur.execute(sql.SQL(stmt).format(ident))

    def rollback_and_verify(self):
        self.conn.rollback()
        self.conn.close()
        with psycopg.connect(URL, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute("select exists (select 1 from pg_namespace where nspname = %s)",
                            [self.schema])
                self.assertFalse(cur.fetchone()[0], f"{self.schema} survived rollback")

    def test_grants_are_open_before_the_lockdown(self):
        found = open_access(self.cur, self.schema)
        for name in ("relations", "functions", "effective", "rls off"):
            self.assertNotEqual(found[name], [], name)
        self.assertTrue(any(scope == "schema" for scope, *_ in found["default privileges"]))

    def test_lockdown_closes_everything_in_the_current_schema(self):
        self.cur.execute(self.lockdown)
        self.assertEqual(open_access(self.cur, self.schema), EMPTY)

    def test_owner_still_reads_and_searches(self):
        self.cur.execute(self.lockdown)
        self.cur.execute("select current_user")
        self.assertEqual(self.cur.fetchone()[0], "postgres")
        self.cur.execute("select count(*) from articles")
        self.assertEqual(self.cur.fetchone()[0], 0)
        self.cur.execute("select count(*) from kb_search_lexical('wifi', 50)")
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_lockdown_is_idempotent(self):
        self.cur.execute(self.lockdown)
        self.cur.execute(self.lockdown)
        self.assertEqual(open_access(self.cur, self.schema), EMPTY)


if __name__ == "__main__":
    unittest.main()
