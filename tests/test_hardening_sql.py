"""Live DB tests for the search_path pinning migration (*_pin_function_search_path.sql).
Skipped unless KB_FINDER_TEST_DATABASE_URL is set (except the offline file check).

Isolation follows test_semantic_sql.py: one transaction per test, always rolled back,
a uniquely named throwaway schema, search_path set to ONLY that schema, then the
pinned migrations and the Phase 7 migrations before the new one. Rows are synthetic:
placeholder articles and unit vectors in the first two coordinates.

The new migration must change only proconfig: same prosrc, same rows, ranks and exact
score text for both search functions, as postgres and as kb_app, the same KBF
SQLSTATEs, and the session search_path left as it was.
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import ingest_kb  # noqa: E402
import test_search_sql as base  # noqa: E402  (helpers only; its TestCase is not imported)
from test_semantic_sql import DIMS, article_v1_sha256, literal, vec  # noqa: E402

URL = os.environ.get("KB_FINDER_TEST_DATABASE_URL")
MIGRATION_DIR = ROOT / "supabase" / "migrations"
ARTICLES_MIGRATION = MIGRATION_DIR / "20260930042716_create_articles.sql"
SEMANTIC_MIGRATION = MIGRATION_DIR / "20261001011735_add_semantic_embeddings.sql"
LOCKDOWN_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_lock_down_data_api.sql"))
APP_ROLE_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_add_app_role.sql"))
PIN_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_pin_function_search_path.sql"))

APP_ROLE = "kb_app"
CFG = "a" * 64
KB_FUNCTIONS = ("kb_article_v1_sha256", "kb_join_tags", "kb_lexical_tsquery",
                "kb_search_lexical", "kb_search_semantic")
LEXICAL_QUERY = "Cannot connect to campus wifi after password reset"
LEXICAL = "select rank, source_article_id, score::text from kb_search_lexical(%s, 50)"
SEMANTIC = ("select rank, source_article_id, score::text from "
            "kb_search_semantic(%s::text::extensions.vector, %s::int, %s::text)")

# (title, full text, tags, angle of the unit embedding). 4 and 5 tie in both modes.
ARTICLES = {
    "1": ("Wifi password reset", "Reset your password, then reconnect to campus wifi.",
          ["wifi"], 10),
    "2": ("Campus wifi", "Connect to the campus wifi network.", [], 35),
    "3": ("Password reset", "Reset a forgotten password.", ["password"], 80),
    "4": ("Printing", "Connect to a campus printer.", [], 60),
    "5": ("Printing guide", "Connect to a campus printer.", [], 60),
    "6": ("Duo enrollment", "Enroll a phone in Duo.", [], 170),
}

if URL:
    import psycopg
    from psycopg import sql


class PinMigrationFileTest(unittest.TestCase):
    """Offline."""

    def test_one_pin_migration_after_the_app_role(self):
        self.assertEqual(len(PIN_MIGRATIONS), 1, PIN_MIGRATIONS)
        self.assertEqual(len(APP_ROLE_MIGRATIONS), 1, APP_ROLE_MIGRATIONS)
        self.assertGreater(PIN_MIGRATIONS[0].name, APP_ROLE_MIGRATIONS[0].name)

    def test_no_function_is_redefined(self):
        lines = PIN_MIGRATIONS[0].read_text(encoding="utf-8").lower().splitlines()
        text = "\n".join(line for line in lines if not line.lstrip().startswith("--"))
        self.assertNotIn("create or replace", text)
        self.assertNotIn("create function", text)


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class PinSearchPathMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert len(PIN_MIGRATIONS) == 1, PIN_MIGRATIONS
        cls.migrations = [p.read_text(encoding="utf-8") for p in
                          (ARTICLES_MIGRATION, SEMANTIC_MIGRATION, LOCKDOWN_MIGRATIONS[0],
                           APP_ROLE_MIGRATIONS[0])]
        cls.pin = PIN_MIGRATIONS[0].read_text(encoding="utf-8")
        with psycopg.connect(URL, autocommit=True) as c:
            cls.public_before = (base.public_snapshot(c), function_state(c.cursor(), "public"))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(URL, autocommit=True) as c:
            after = (base.public_snapshot(c), function_state(c.cursor(), "public"))
        assert after == cls.public_before, "public functions changed"

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

    # -------------------------------------------------------------- helpers

    def load_synthetic_rows(self):
        recs = [base.article(sid, title=title, full_text=text, tags=tags)
                for sid, (title, text, tags, _) in ARTICLES.items()]
        ingest_kb.sync(self.conn, base.rows(*recs), False)
        self.cur.execute("insert into embedding_configs (config_sha256, config, model, dimensions, "
                         "template_version) values (%s, '{}'::jsonb, 'text-embedding-3-small', "
                         "%s, 'article-v1')", [CFG, DIMS])
        for sid, (title, text, _, angle) in ARTICLES.items():
            self.cur.execute(
                "insert into article_embeddings (config_sha256, source_article_id, "
                "source_text_sha256, input_kind, input_sha256, input_tokens, truncated, embedding) "
                "values (%s, %s, %s, 'text', %s, 10, false, %s::text::extensions.vector)",
                [CFG, sid, article_v1_sha256(title, text), "c" * 64, literal(vec(angle))])

    def searches(self):
        self.cur.execute(LEXICAL, [LEXICAL_QUERY])
        lexical = self.cur.fetchall()
        self.cur.execute(SEMANTIC, [literal(vec(25)), 50, CFG])
        semantic = self.cur.fetchall()
        return lexical, semantic

    def kbf_codes(self):
        cases = {"KBF01": [literal(vec(0)), 51, CFG],
                 "KBF02": [literal(vec(0)), 50, "b" * 64],
                 "KBF03": [literal([0.0] * DIMS), 50, CFG]}
        got = {}
        for name, args in cases.items():
            with self.assertRaises(psycopg.Error, msg=name) as ctx:
                with self.conn.transaction():  # savepoint: only this statement rolls back
                    self.cur.execute(SEMANTIC, args)
            got[name] = ctx.exception.sqlstate
        return got

    def search_path(self):
        self.cur.execute("select current_setting('search_path')")
        return self.cur.fetchone()[0]

    def as_app(self):
        self.cur.execute("set local role kb_app")
        self.cur.execute("select current_user")
        self.assertEqual(self.cur.fetchone()[0], APP_ROLE)

    # -------------------------------------------------------------- tests

    def test_results_are_unchanged_as_owner_and_as_kb_app(self):
        before = self.searches()
        lexical, semantic = before
        self.assertEqual([r[1] for r in lexical], ["1", "2", "3", "4", "5"])
        self.assertEqual([r[1] for r in semantic], ["2", "1", "4", "5", "3", "6"])
        self.assertEqual(lexical[3][2], lexical[4][2])    # tie broken on id
        self.assertEqual(semantic[2][2], semantic[3][2])
        codes = self.kbf_codes()
        self.assertEqual(codes, {"KBF01": "KBF01", "KBF02": "KBF02", "KBF03": "KBF03"})

        self.cur.execute(self.pin)
        self.assertEqual(self.searches(), before)
        self.assertEqual(self.kbf_codes(), codes)
        self.as_app()
        self.assertEqual(self.searches(), before)
        self.assertEqual(self.kbf_codes(), codes)

    def test_only_proconfig_changes_and_session_path_is_restored(self):
        path_before = self.search_path()
        self.assertEqual(path_before, self.schema)
        state_before = function_state(self.cur, self.schema)
        self.assertEqual(sorted(state_before), list(KB_FUNCTIONS))
        self.assertTrue(all(cfg is None for _, cfg in state_before.values()))

        self.cur.execute(self.pin)
        self.assertEqual(self.search_path(), path_before)
        state_after = function_state(self.cur, self.schema)
        self.assertEqual({k: src for k, (src, _) in state_after.items()},
                         {k: src for k, (src, _) in state_before.items()})
        for name, (_, cfg) in state_after.items():
            self.assertEqual(cfg, [f"search_path={self.schema}, pg_temp"], name)

    def test_pinned_functions_ignore_the_caller_search_path(self):
        before = self.searches()
        self.cur.execute(self.pin)
        self.cur.execute("set local search_path to pg_catalog")
        ident = sql.Identifier(self.schema)
        self.cur.execute(sql.SQL("select rank, source_article_id, score::text "
                                 "from {}.kb_search_lexical(%s, 50)").format(ident),
                         [LEXICAL_QUERY])
        lexical = self.cur.fetchall()
        self.cur.execute(sql.SQL("select rank, source_article_id, score::text from "
                                 "{}.kb_search_semantic(%s::text::extensions.vector, 50, %s)"
                                 ).format(ident), [literal(vec(25)), CFG])
        self.assertEqual((lexical, self.cur.fetchall()), before)

    def test_unpinned_functions_follow_the_caller_search_path(self):
        # The control for the test above: without the migration, the same call fails.
        self.cur.execute("set local search_path to pg_catalog")
        with self.assertRaises(psycopg.errors.ProgrammingError) as ctx:
            with self.conn.transaction():
                self.cur.execute(sql.SQL("select * from {}.kb_search_lexical(%s, 50)").format(
                    sql.Identifier(self.schema)), [LEXICAL_QUERY])
        # kb_lexical_tsquery or articles, whichever the body resolves first.
        self.assertIn(ctx.exception.sqlstate, {"42883", "42P01"})

    def test_pin_migration_is_idempotent(self):
        before = self.searches()
        self.cur.execute(self.pin)
        state = function_state(self.cur, self.schema)
        self.cur.execute(self.pin)
        self.assertEqual(function_state(self.cur, self.schema), state)
        self.assertEqual(self.searches(), before)


def function_state(cur, ns):
    """{proname: (prosrc, proconfig)} for the kb_ functions in ns."""
    cur.execute("select proname, prosrc, proconfig from pg_proc "
                "where pronamespace = %s::regnamespace and proname like 'kb\\_%%' order by 1",
                [ns])
    return {name: (src, cfg) for name, src, cfg in cur.fetchall()}


if __name__ == "__main__":
    unittest.main()
