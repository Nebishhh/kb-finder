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
APP_ROLE_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_add_app_role.sql"))

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

    def test_one_app_role_migration_after_the_lockdown(self):
        self.assertEqual(len(APP_ROLE_MIGRATIONS), 1, APP_ROLE_MIGRATIONS)
        self.assertGreater(APP_ROLE_MIGRATIONS[0].name, LOCKDOWN_MIGRATIONS[0].name)


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
        for name in ("article_embeddings", "articles", "embedding_configs", "kb_rate_counters"):
            self.assertIn(name, tables)
        self.cur.execute("select count(*) from pg_proc where pronamespace = 'public'::regnamespace "
                         "and proname like 'kb\\_%'")
        self.assertEqual(self.cur.fetchone()[0], 8)  # 5 v1 + 3 from *_add_article_v2.sql

    def test_kb_functions_pin_search_path(self):
        # Step 2 (*_pin_function_search_path.sql) and *_add_article_v2.sql: every kb_
        # function in public.
        self.cur.execute("select proname, proconfig from pg_proc "
                         "where pronamespace = 'public'::regnamespace and proname like 'kb\\_%' "
                         "order by 1")
        got = self.cur.fetchall()
        self.assertEqual([name for name, _ in got],
                         ["kb_article_v1_sha256", "kb_article_v2_sha256", "kb_join_tags",
                          "kb_lexical_tsquery", "kb_search_lexical", "kb_search_lexical_v2",
                          "kb_search_semantic", "kb_search_semantic_v2"])
        for name, config in got:
            self.assertEqual(config, ["search_path=public, extensions, pg_temp"], name)


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


# ------------------------------------------------------------------ kb_app (step 3)

APP_ROLE = "kb_app"

# Exactly what kb_app is granted, as (schema-relative object, privilege).
APP_TABLE_GRANTS = {
    ("article_embeddings", "SELECT"),
    ("articles", "SELECT"),
    ("embedding_configs", "SELECT"),
    ("kb_rate_counters", "DELETE"),
    ("kb_rate_counters", "INSERT"),
    ("kb_rate_counters", "SELECT"),
    ("kb_rate_counters", "UPDATE"),
}
APP_FUNCTION_GRANTS = {
    "kb_article_v1_sha256(pg_catalog.text,pg_catalog.text)",
    "kb_lexical_tsquery(pg_catalog.text)",
    "kb_search_lexical(pg_catalog.text,pg_catalog.int4)",
    "kb_search_semantic(extensions.vector,pg_catalog.int4,pg_catalog.text)",
    # *_add_article_v2.sql
    "kb_article_v2_sha256(pg_catalog.text,pg_catalog.text,pg_catalog.jsonb)",
    "kb_search_lexical_v2(pg_catalog.text,pg_catalog.int4)",
    "kb_search_semantic_v2(extensions.vector,pg_catalog.int4,pg_catalog.text)",
}
APP_SCHEMA_GRANTS = {("extensions", "USAGE"), ("public", "USAGE")}
APP_POLICIES = {
    ("article_embeddings", "kb_app_select", "r", "true", None),
    ("articles", "kb_app_select", "r", "true", None),
    ("embedding_configs", "kb_app_select", "r", "true", None),
    ("kb_rate_counters", "kb_app_all", "*", "true", "true"),
}
ALL_TABLE_PRIVS = TABLE_PRIVS + ("MAINTAIN",)


# name(schema.type,...) with every argument type schema-qualified, so the text does
# not depend on the session's search_path (regprocedure output does).
SIGNATURE = """p.proname || '(' || coalesce((
  select string_agg(tn.nspname || '.' || t.typname, ',' order by a.i)
  from unnest(p.proargtypes) with ordinality as a(oid, i)
  join pg_type t on t.oid = a.oid join pg_namespace tn on tn.oid = t.typnamespace), '') || ')'"""


def app_grants(cur, ns):
    """kb_app's direct grants anywhere in the database, and its effective rights in ns."""
    cur.execute("""
        select n.nspname, c.relname, a.privilege_type
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        cross join lateral aclexplode(c.relacl) a
        where a.grantee = %(role)s::regrole order by 1, 2, 3""", {"role": APP_ROLE})
    direct_relations = cur.fetchall()
    cur.execute("""
        select c.relname, att.attname, a.privilege_type
        from pg_attribute att join pg_class c on c.oid = att.attrelid
        cross join lateral aclexplode(att.attacl) a
        where att.attacl is not null and a.grantee = %(role)s::regrole order by 1, 2, 3""",
                {"role": APP_ROLE})
    direct_columns = cur.fetchall()
    cur.execute(f"""
        select n.nspname, {SIGNATURE}
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        cross join lateral aclexplode(p.proacl) a
        where a.grantee = %(role)s::regrole order by 1, 2""", {"role": APP_ROLE})
    direct_functions = cur.fetchall()
    cur.execute("""
        select n.nspname, a.privilege_type from pg_namespace n
        cross join lateral aclexplode(n.nspacl) a
        where a.grantee = %(role)s::regrole order by 1, 2""", {"role": APP_ROLE})
    direct_schemas = cur.fetchall()
    cur.execute("""
        select c.relname, p from pg_class c, unnest(%(privs)s::text[]) p
        where c.relnamespace = %(ns)s::regnamespace and c.relkind in ('r', 'p', 'v', 'm', 'f')
          and has_table_privilege(%(role)s, c.oid, p)
        union all
        select c.relname, p from pg_class c, unnest(%(seq)s::text[]) p
        where c.relnamespace = %(ns)s::regnamespace and c.relkind = 'S'
          and has_sequence_privilege(%(role)s, c.oid, p)
        order by 1, 2""", {"role": APP_ROLE, "ns": ns, "privs": list(ALL_TABLE_PRIVS),
                           "seq": list(SEQUENCE_PRIVS)})
    effective_relations = cur.fetchall()
    cur.execute(f"""
        select {SIGNATURE} from pg_proc p
        where p.pronamespace = %(ns)s::regnamespace
          and has_function_privilege(%(role)s, p.oid, 'EXECUTE') order by 1""",
                {"role": APP_ROLE, "ns": ns})
    effective_functions = [r[0] for r in cur.fetchall()]
    cur.execute("""
        select n.nspname from pg_namespace n
        where has_schema_privilege(%(role)s, n.oid, 'CREATE') order by 1""", {"role": APP_ROLE})
    create_schemas = [r[0] for r in cur.fetchall()]
    cur.execute("select has_database_privilege(%s, current_database(), 'CREATE')", [APP_ROLE])
    create_database = cur.fetchone()[0]
    cur.execute("select pg_get_userbyid(roleid) from pg_auth_members "
                "where member = %s::regrole order by 1", [APP_ROLE])
    member_of = [r[0] for r in cur.fetchall()]
    return {
        "direct_relations": direct_relations, "direct_columns": direct_columns,
        "direct_functions": direct_functions, "direct_schemas": direct_schemas,
        "effective_relations": effective_relations, "effective_functions": effective_functions,
        "create_schemas": create_schemas, "create_database": create_database,
        "member_of": member_of,
    }


def app_policies(cur, ns):
    cur.execute("""
        select c.relname, pol.polname, pol.polcmd::text,
               pg_get_expr(pol.polqual, pol.polrelid), pg_get_expr(pol.polwithcheck, pol.polrelid),
               pol.polpermissive, array(select pg_get_userbyid(r) from unnest(pol.polroles) r)
        from pg_policy pol join pg_class c on c.oid = pol.polrelid
        where c.relnamespace = %s::regnamespace order by 1, 2""", [ns])
    return cur.fetchall()


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class AppRoleCatalogTests(unittest.TestCase):
    """Catalog reads only, read-only transaction, against the migrated database."""

    def setUp(self):
        self.conn = psycopg.connect(URL)
        self.conn.read_only = True
        self.addCleanup(self.conn.close)
        self.cur = self.conn.cursor()

    def test_role_attributes_and_settings(self):
        self.cur.execute("select rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolbypassrls, "
                         "rolreplication, rolconfig from pg_roles where rolname = %s", [APP_ROLE])
        row = self.cur.fetchone()
        self.assertIsNotNone(row, "kb_app does not exist; apply the migrations")
        *flags, config = row
        self.assertEqual(flags, [False] * 6)  # super, inherit, createrole, createdb, bypassrls, repl
        self.assertIn("statement_timeout=5000ms", config)
        self.assertIn("search_path=public, extensions", config)

    def test_exact_grants(self):
        got = app_grants(self.cur, "public")
        self.assertEqual(set(got["direct_relations"]),
                         {("public", rel, priv) for rel, priv in APP_TABLE_GRANTS})
        self.assertEqual(got["direct_columns"], [])
        self.assertEqual(set(got["direct_functions"]),
                         {("public", f) for f in APP_FUNCTION_GRANTS})
        self.assertEqual(set(got["direct_schemas"]), APP_SCHEMA_GRANTS)
        self.assertEqual(set(got["effective_relations"]), APP_TABLE_GRANTS)
        self.assertEqual(set(got["effective_functions"]), APP_FUNCTION_GRANTS)
        self.assertEqual(got["create_schemas"], [])
        self.assertFalse(got["create_database"])
        self.assertEqual(got["member_of"], [])

    def test_policies_are_only_for_kb_app(self):
        got = app_policies(self.cur, "public")
        self.assertEqual({row[:5] for row in got}, APP_POLICIES)
        for row in got:
            self.assertTrue(row[5], row)              # permissive
            self.assertEqual(row[6], [APP_ROLE], row)  # roles


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class AppRoleMigrationTests(unittest.TestCase):
    """All four migrations in a throwaway schema, synthetic rows, then SET LOCAL ROLE
    kb_app. One transaction, always rolled back."""

    CFG = "a" * 64
    DIMS = 1536
    LEXICAL = "select rank, source_article_id, score::text from kb_search_lexical(%s, 50)"
    SEMANTIC = ("select rank, source_article_id, score::text from "
                "kb_search_semantic(%s::text::extensions.vector, 50, %s)")

    @classmethod
    def setUpClass(cls):
        assert len(APP_ROLE_MIGRATIONS) == 1, APP_ROLE_MIGRATIONS
        cls.migrations = [ARTICLES_MIGRATION.read_text(encoding="utf-8"),
                          SEMANTIC_MIGRATION.read_text(encoding="utf-8"),
                          LOCKDOWN_MIGRATIONS[0].read_text(encoding="utf-8")]
        cls.app_role = APP_ROLE_MIGRATIONS[0].read_text(encoding="utf-8")
        with psycopg.connect(URL, autocommit=True) as c:
            cls.public_before = (base.public_snapshot(c), open_access(c.cursor(), "public"),
                                 app_grants(c.cursor(), "public"))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(URL, autocommit=True) as c:
            after = (base.public_snapshot(c), open_access(c.cursor(), "public"),
                     app_grants(c.cursor(), "public"))
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
        cur.execute(self.app_role)
        self.load_synthetic_rows()
        # postgres holds ADMIN on kb_app but not SET; this grant lives only in this
        # transaction.
        cur.execute("grant kb_app to current_user with inherit false, set true")

    def rollback_and_verify(self):
        self.conn.rollback()
        self.conn.close()
        with psycopg.connect(URL, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute("select exists (select 1 from pg_namespace where nspname = %s)",
                            [self.schema])
                self.assertFalse(cur.fetchone()[0], f"{self.schema} survived rollback")

    def unit_literal(self, index):
        v = ["0"] * self.DIMS
        v[index] = "1"
        return "[" + ",".join(v) + "]"

    def load_synthetic_rows(self):
        cur = self.cur
        cur.execute(
            "insert into articles (source_article_id, title, url, full_text, sections, parser_version) "
            "select id, 'Wifi heading ' || id, "
            "'https://td.usd.edu/TDClient/33/Portal/KB/Article/' || id || '/x', "
            "'Connect to wifi. Body ' || id || '.', '[]', '2' "
            "from unnest(array['9', '10', '100']) as id")
        cur.execute("insert into embedding_configs values (%s, '{}', 'text-embedding-3-small', "
                    "%s, 'article-v1')", [self.CFG, self.DIMS])
        for sid, index in (("9", 1), ("10", 0), ("100", 2)):
            cur.execute(
                "insert into article_embeddings (config_sha256, source_article_id, "
                "source_text_sha256, input_kind, input_sha256, input_tokens, truncated, embedding) "
                "select %s, a.source_article_id, kb_article_v1_sha256(a.title, a.full_text), "
                "'text', repeat('c', 64), 5, false, %s::text::extensions.vector "
                "from articles a where a.source_article_id = %s",
                [self.CFG, self.unit_literal(index), sid])

    def searches(self):
        self.cur.execute(self.LEXICAL, ["wifi"])
        lexical = self.cur.fetchall()
        self.cur.execute(self.SEMANTIC, [self.unit_literal(0), self.CFG])
        semantic = self.cur.fetchall()
        return lexical, semantic

    def as_app(self):
        self.cur.execute("set local role kb_app")
        self.cur.execute("select current_user")
        self.assertEqual(self.cur.fetchone()[0], APP_ROLE)

    def assert_denied(self, statement, args=None):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=statement):
            with self.conn.transaction():  # savepoint: only this statement rolls back
                self.cur.execute(statement, args)

    def test_kb_app_searches_return_the_owner_rows(self):
        owner = self.searches()
        self.assertEqual(len(owner[0]), 3)
        self.assertEqual([r[1] for r in owner[1]], ["10", "100", "9"])
        self.as_app()
        self.assertEqual(self.searches(), owner)

    def test_kb_app_reads_and_writes_rate_counters(self):
        self.as_app()
        key = "b" * 64
        self.cur.execute("insert into kb_rate_counters values ('search_ip_10m', %s, "
                         "'2026-10-01T00:00Z', 1)", [key])
        self.cur.execute("insert into kb_rate_counters values ('search_global_day', 'all', "
                         "'2026-10-01T00:00Z', 1)")
        self.cur.execute("update kb_rate_counters set count = count + 1 "
                         "where kind = 'search_ip_10m' and key = %s returning count", [key])
        self.assertEqual(self.cur.fetchone()[0], 2)
        self.cur.execute("select count(*) from kb_rate_counters")
        self.assertEqual(self.cur.fetchone()[0], 2)
        self.cur.execute("delete from kb_rate_counters where window_start < '2026-10-02T00:00Z'")
        self.assertEqual(self.cur.rowcount, 2)

    def test_rate_counter_checks(self):
        for kind, key, count in (("search_ip_10m", "all", 1),            # 'all' only for global
                                 ("search_global_day", "c" * 64, 1),     # global must be 'all'
                                 ("search_ip_10m", "C" * 64, 1),         # lowercase hex only
                                 ("search_ip_10m", "c" * 63, 1),
                                 ("raw_ip", "c" * 64, 1),
                                 ("search_ip_10m", "c" * 64, 0)):
            with self.assertRaises(psycopg.errors.CheckViolation, msg=(kind, key, count)):
                with self.conn.transaction():
                    self.cur.execute("insert into kb_rate_counters values (%s, %s, now(), %s)",
                                     [kind, key, count])

    def test_kb_app_is_denied_everything_else(self):
        self.as_app()
        self.assert_denied(
            "insert into articles (source_article_id, title, url, full_text, sections, "
            "parser_version) values ('11', 't', "
            "'https://td.usd.edu/TDClient/33/Portal/KB/Article/11/x', 'b', '[]', '2')")
        self.assert_denied("update articles set title = 'x'")
        self.assert_denied("delete from article_embeddings")
        self.assert_denied("insert into embedding_configs values (%s, '{}', 'm', 1536, 'v')",
                           ["d" * 64])
        self.assert_denied("truncate kb_rate_counters")
        self.assert_denied("select kb_join_tags(array['a'])")
        self.assert_denied(sql.SQL("create table {}.kb_app_probe (x int)").format(
            sql.Identifier(self.schema)))

    def test_app_role_migration_is_idempotent(self):
        owner = self.searches()
        self.cur.execute(self.app_role)
        self.as_app()
        self.assertEqual(self.searches(), owner)


if __name__ == "__main__":
    unittest.main()
