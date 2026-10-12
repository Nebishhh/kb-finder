"""Tests for the article-v2 migration (*_add_article_v2.sql): search_vector_v2,
kb_search_lexical_v2, kb_article_v2_sha256 and kb_search_semantic_v2.

Offline: the migration exists once, sorts last, and redefines nothing.

Live (skipped unless KB_FINDER_TEST_DATABASE_URL is set). Isolation follows
test_hardening_sql.py: one transaction per test, always rolled back, a uniquely named
throwaway schema, search_path set to ONLY that schema, every earlier migration run in
order, synthetic rows loaded, and only then the article-v2 migration (so the generated
column is computed on existing rows, as on the local dev database). Rows and vectors
are synthetic placeholders.
"""
import hashlib
import json
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
ALL_MIGRATIONS = sorted(MIGRATION_DIR.glob("*.sql"))
V2_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_add_article_v2.sql"))
GATE_KIND_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_add_gate_fail_kind.sql"))

APP_ROLE = "kb_app"
CLOSED_ROLES = ("anon", "authenticated")  # plus PUBLIC
V1_FUNCTIONS = ("kb_article_v1_sha256", "kb_join_tags", "kb_lexical_tsquery",
                "kb_search_lexical", "kb_search_semantic")
NEW_FUNCTIONS = {
    "kb_search_lexical_v2": "kb_search_lexical",
    "kb_article_v2_sha256": "kb_article_v1_sha256",
    "kb_search_semantic_v2": "kb_search_semantic",
}
CFG_V1 = "a" * 64
CFG_V2 = "b" * 64

LEXICAL = "select rank, source_article_id, score::text from kb_search_lexical(%s, 50)"
LEXICAL_V2 = "select rank, source_article_id, score::text from kb_search_lexical_v2(%s, 50)"
SEMANTIC = ("select rank, source_article_id, score::text from "
            "kb_search_semantic(%s::text::extensions.vector, %s::int, %s::text)")
SEMANTIC_V2 = ("select rank, source_article_id, score::text from "
               "kb_search_semantic_v2(%s::text::extensions.vector, %s::int, %s::text)")
V1_QUERIES = ("Cannot connect to campus wifi after password reset", "zebrafinch",
              "printer queue", "escalate")

# (title, full text, staff text or None, angle of the unit embedding).
ARTICLES = {
    "1": ("Wifi password reset", "Reset your password, then reconnect to campus wifi.",
          None, 10),
    "2": ("Printer setup", "Add a campus printer.", "Escalate to the zebrafinch queue.", 35),
    "3": ("Queue help", "The zebrafinch queue handles printer tickets.", None, 80),
    "4": ("Duo enrollment", "Enroll a phone in Duo.", "Café – naïve 日本 reset", 170),
}

# The six characters the article-v2 trim rule removes (SQL comment in the migration).
TRIM = " \t\n\x0b\x0c\r"

# kb_article_v2_sha256 edge cases: (staff_only as JSON text or None for SQL NULL,
# suffix expected). Shared with tests/test_embed_kb.py, which checks the
# pipeline's builder on the same cases.
STAFF_CASES = [
    (None, False),
    ("null", False),
    ("{}", False),
    ('{"links": []}', False),
    ('{"text": null, "links": []}', False),
    ('{"text": "", "links": []}', False),
    (json.dumps({"text": TRIM, "links": []}), False),
    (json.dumps({"text": " \n\t ", "links": []}), False),
    (json.dumps({"text": "\xa0", "links": []}), True),        # NBSP is content
    (json.dumps({"text": " ", "links": []}), True),      # em space is content
    (json.dumps({"text": "Café – naïve 日本 😀"}), True),
    (json.dumps({"text": "Café – naïve 日本", "links": []}, ensure_ascii=False), True),
    (json.dumps({"text": "  padded\r\n", "links": []}), True),  # kept untrimmed
    (json.dumps({"text": "é"}), True),                  # not NFC-normalized
]

if URL:
    import psycopg
    from psycopg import sql


def article_v2_text(title, full_text, staff_only):
    """Reference for the article-v2 contract, written from the migration comment."""
    text = f"Title: {title}\n\n{full_text}"
    staff = staff_only.get("text") if isinstance(staff_only, dict) else None
    if isinstance(staff, str) and staff.strip(TRIM) != "":
        text += "\n\nStaff only:\n" + staff
    return text


def article_v2_sha256(title, full_text, staff_only):
    return hashlib.sha256(article_v2_text(title, full_text, staff_only).encode("utf-8")).hexdigest()


def code_lines(path):
    lines = path.read_text(encoding="utf-8").lower().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("--"))


class ArticleV2MigrationFileTest(unittest.TestCase):
    """Offline."""

    def test_one_v2_migration_and_it_sorts_last(self):
        self.assertEqual(len(V2_MIGRATIONS), 1, V2_MIGRATIONS)
        self.assertEqual(len(GATE_KIND_MIGRATIONS), 1, GATE_KIND_MIGRATIONS)
        self.assertEqual(ALL_MIGRATIONS[-1], V2_MIGRATIONS[0])
        self.assertGreater(V2_MIGRATIONS[0].name, GATE_KIND_MIGRATIONS[0].name)

    def test_adds_only(self):
        text = code_lines(V2_MIGRATIONS[0])
        self.assertNotIn("create or replace", text)
        self.assertNotIn("drop ", text)
        self.assertNotIn("alter column", text)
        self.assertNotIn("security definer", text)
        for name in V1_FUNCTIONS:
            self.assertNotIn(f"create function {name}(", text, name)
            self.assertNotIn(f"alter function %i.{name}(", text, name)
        for name in NEW_FUNCTIONS:
            self.assertEqual(text.count(f"create function {name}("), 1, name)

    def test_reference_builder(self):
        # The Python reference itself, with no database: bytes, not just hashes.
        self.assertEqual(article_v2_text("T", "B", None), "Title: T\n\nB")
        self.assertEqual(article_v2_text("T", "B", {"text": " x "}),
                         "Title: T\n\nB\n\nStaff only:\n x ")
        self.assertEqual(article_v2_text("T", "B", {"text": TRIM}), "Title: T\n\nB")
        self.assertEqual(article_v2_sha256("T", "B", None), article_v1_sha256("T", "B"))


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class ArticleV2MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert len(V2_MIGRATIONS) == 1, V2_MIGRATIONS
        cls.earlier = [p.read_text(encoding="utf-8") for p in ALL_MIGRATIONS
                       if p != V2_MIGRATIONS[0]]
        cls.v2 = V2_MIGRATIONS[0].read_text(encoding="utf-8")
        with psycopg.connect(URL, autocommit=True) as c:
            cls.public_before = base.public_snapshot(c)

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(URL, autocommit=True) as c:
            after = base.public_snapshot(c)
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
        for migration in self.earlier:
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
        recs = [base.article(sid, title=title, full_text=text, staff_text=staff)
                for sid, (title, text, staff, _) in ARTICLES.items()]
        ingest_kb.sync(self.conn, base.rows(*recs), False)
        self.cur.execute("insert into embedding_configs (config_sha256, config, model, dimensions, "
                         "template_version) values (%s, '{}'::jsonb, 'text-embedding-3-small', "
                         "%s, 'article-v1')", [CFG_V1, DIMS])
        for sid, (title, text, _, angle) in ARTICLES.items():
            self.insert_embedding(CFG_V1, sid, article_v1_sha256(title, text), angle)

    def insert_embedding(self, cfg, sid, text_sha256, angle):
        self.cur.execute(
            "insert into article_embeddings (config_sha256, source_article_id, "
            "source_text_sha256, input_kind, input_sha256, input_tokens, truncated, embedding) "
            "values (%s, %s, %s, 'text', %s, 10, false, %s::text::extensions.vector)",
            [cfg, sid, text_sha256, "c" * 64, literal(vec(angle))])

    def load_v2_config(self, cfg=CFG_V2, template_version="article-v2"):
        self.cur.execute("insert into embedding_configs (config_sha256, config, model, dimensions, "
                         "template_version) values (%s, '{}'::jsonb, 'text-embedding-3-small', "
                         "%s, %s)", [cfg, DIMS, template_version])
        for sid, (title, text, staff, angle) in ARTICLES.items():
            staff_only = None if staff is None else {"text": staff, "links": []}
            self.insert_embedding(cfg, sid, article_v2_sha256(title, text, staff_only), angle)

    def v1_searches(self):
        got = []
        for q in V1_QUERIES:
            self.cur.execute(LEXICAL, [q])
            got.append(self.cur.fetchall())
        self.cur.execute(SEMANTIC, [literal(vec(25)), 50, CFG_V1])
        got.append(self.cur.fetchall())
        return got

    def ids(self, query, q):
        self.cur.execute(query, [q])
        return [r[1] for r in self.cur.fetchall()]

    def sqlstate(self, query, args):
        with self.assertRaises(psycopg.Error) as ctx:
            with self.conn.transaction():  # savepoint: only this statement rolls back
                self.cur.execute(query, args)
        return ctx.exception.sqlstate

    def as_app(self):
        self.cur.execute("set local role kb_app")
        self.cur.execute("select current_user")
        self.assertEqual(self.cur.fetchone()[0], APP_ROLE)

    def function_rows(self, names):
        self.cur.execute("select proname, prosecdef, provolatile::text, proconfig, proacl is null "
                         "from pg_proc where pronamespace = %s::regnamespace "
                         "and proname = any(%s) order by 1", [self.schema, list(names)])
        return {r[0]: r[1:] for r in self.cur.fetchall()}

    # -------------------------------------------------------------- tests

    def test_v1_results_are_unchanged(self):
        before = self.v1_searches()
        self.assertEqual([r[1] for r in before[0]], ["1", "2"])  # not vacuous
        self.cur.execute(self.v2)
        self.assertEqual(self.v1_searches(), before)
        self.as_app()
        self.assertEqual(self.v1_searches(), before)

    def test_v1_column_and_functions_are_untouched(self):
        self.cur.execute("select source_article_id, search_vector::text from articles order by 1")
        vectors = self.cur.fetchall()
        self.cur.execute("select proname, prosrc, proconfig, proacl::text from pg_proc "
                         "where pronamespace = %s::regnamespace and proname = any(%s) order by 1",
                         [self.schema, list(V1_FUNCTIONS)])
        functions = self.cur.fetchall()
        self.assertEqual(len(functions), len(V1_FUNCTIONS))
        self.cur.execute(self.v2)
        self.cur.execute("select source_article_id, search_vector::text from articles order by 1")
        self.assertEqual(self.cur.fetchall(), vectors)
        self.cur.execute("select proname, prosrc, proconfig, proacl::text from pg_proc "
                         "where pronamespace = %s::regnamespace and proname = any(%s) order by 1",
                         [self.schema, list(V1_FUNCTIONS)])
        self.assertEqual(self.cur.fetchall(), functions)

    def test_lexical_v2_matches_staff_only_terms(self):
        self.cur.execute(self.v2)
        # "escalate" is only in article 2's staff text.
        self.assertEqual(self.ids(LEXICAL, "escalate"), [])
        self.assertEqual(self.ids(LEXICAL_V2, "escalate"), ["2"])
        # "zebrafinch" is in article 3's body and article 2's staff text (weight D).
        self.assertEqual(self.ids(LEXICAL, "zebrafinch"), ["3"])
        self.assertEqual(self.ids(LEXICAL_V2, "zebrafinch"), ["3", "2"])
        # Non-ASCII staff text is indexed.
        self.assertEqual(self.ids(LEXICAL_V2, "café"), ["4"])
        self.assertEqual(self.ids(LEXICAL, "café"), [])
        # A query that touches no staff text: identical rows, ranks and scores.
        q = "campus wifi"
        self.cur.execute(LEXICAL, [q])
        v1 = self.cur.fetchall()
        self.assertEqual([r[1] for r in v1], ["1", "2"])
        self.cur.execute(LEXICAL_V2, [q])
        self.assertEqual(self.cur.fetchall(), v1)
        self.as_app()
        self.assertEqual(self.ids(LEXICAL_V2, "escalate"), ["2"])

    def test_search_vector_v2_is_v1_plus_staff_at_weight_d(self):
        self.cur.execute(self.v2)
        self.cur.execute("select count(*) from articles where search_vector_v2 = search_vector")
        self.assertEqual(self.cur.fetchone()[0], 2)  # 1 and 3 have no staff text
        self.cur.execute(
            "select source_article_id from articles where search_vector_v2 = search_vector "
            "|| setweight(to_tsvector('english', coalesce(staff_only->>'text', '')), 'D') "
            "order by 1")
        self.assertEqual([r[0] for r in self.cur.fetchall()], ["1", "2", "3", "4"])
        self.cur.execute("select indexdef from pg_indexes where schemaname = %s "
                         "and indexname = 'articles_search_vector_v2_idx'", [self.schema])
        self.assertIn("USING gin (search_vector_v2)", self.cur.fetchone()[0])

    def test_security_mode_pin_and_grants(self):
        self.cur.execute(self.v2)
        new = self.function_rows(NEW_FUNCTIONS)
        old = self.function_rows(NEW_FUNCTIONS.values())
        self.assertEqual(sorted(new), sorted(NEW_FUNCTIONS))
        for name, v1_name in NEW_FUNCTIONS.items():
            secdef, volatile, config, acl_default = new[name]
            self.assertFalse(secdef, name)                          # SECURITY INVOKER
            self.assertEqual(secdef, old[v1_name][0], name)         # same as its v1 twin
            self.assertEqual(volatile, old[v1_name][1], name)       # same volatility
            self.assertEqual(config, [f"search_path={self.schema}, pg_temp"], name)
            self.assertEqual(config, old[v1_name][2], name)         # same pin as v1
            self.assertFalse(acl_default, name)                     # explicit ACL
        self.cur.execute(
            "select p.proname, case when a.grantee = 0 then 'PUBLIC' "
            "else pg_get_userbyid(a.grantee) end, a.privilege_type "
            "from pg_proc p cross join lateral aclexplode(p.proacl) a "
            "where p.pronamespace = %s::regnamespace and p.proname = any(%s) "
            "and a.grantee <> p.proowner order by 1, 2",
            [self.schema, list(NEW_FUNCTIONS)])
        self.assertEqual(self.cur.fetchall(),
                         [(name, APP_ROLE, "EXECUTE") for name in sorted(NEW_FUNCTIONS)])
        self.cur.execute(
            "select r, p.proname, has_function_privilege(r, p.oid, 'EXECUTE') "
            "from pg_proc p, unnest(%s::text[]) r "
            "where p.pronamespace = %s::regnamespace and p.proname = any(%s) order by 1, 2",
            [list(CLOSED_ROLES) + [APP_ROLE], self.schema, list(NEW_FUNCTIONS)])
        for role, name, allowed in self.cur.fetchall():
            self.assertEqual(allowed, role == APP_ROLE, (role, name))

    def test_pinned_v2_functions_ignore_the_caller_search_path(self):
        self.cur.execute(self.v2)
        self.cur.execute(LEXICAL_V2, ["escalate"])
        before = self.cur.fetchall()
        self.cur.execute("set local search_path to pg_catalog")
        self.cur.execute(sql.SQL("select rank, source_article_id, score::text "
                                 "from {}.kb_search_lexical_v2(%s, 50)").format(
                                     sql.Identifier(self.schema)), ["escalate"])
        self.assertEqual(self.cur.fetchall(), before)

    def test_article_v2_sha256_contract(self):
        self.cur.execute(self.v2)
        title, body = "Wi-Fi help", "Line one.\nLine two."
        cases = STAFF_CASES  # module level, shared with test_embed_kb.py
        for staff_json, has_suffix in cases:
            with self.subTest(staff=staff_json):
                staff = None if staff_json is None else json.loads(staff_json)
                self.cur.execute("select kb_article_v2_sha256(%s, %s, %s::jsonb), "
                                 "kb_article_v1_sha256(%s, %s)",
                                 [title, body, staff_json, title, body])
                v2, v1 = self.cur.fetchone()
                self.assertEqual(v2, article_v2_sha256(title, body, staff))
                self.assertEqual(v2 == v1, not has_suffix)
        # Untrimmed means byte for byte: padding changes the hash.
        self.cur.execute("select kb_article_v2_sha256('T', 'B', '{\"text\": \"x\"}'), "
                         "kb_article_v2_sha256('T', 'B', '{\"text\": \" x \"}')")
        a, b = self.cur.fetchone()
        self.assertNotEqual(a, b)
        # The real rows: SQL agrees with the reference for every article.
        self.cur.execute("select source_article_id, title, full_text, staff_only::text, "
                         "kb_article_v2_sha256(title, full_text, staff_only) from articles")
        for sid, t, f, s, got in self.cur.fetchall():
            self.assertEqual(got, article_v2_sha256(t, f, None if s is None else json.loads(s)), sid)

    def test_semantic_v2_ranks_and_fails_loudly(self):
        self.cur.execute(self.v2)
        self.load_v2_config()
        self.cur.execute(SEMANTIC_V2, [literal(vec(25)), 50, CFG_V2])
        v2 = self.cur.fetchall()
        self.assertEqual([r[1] for r in v2], ["2", "1", "3", "4"])
        self.cur.execute(SEMANTIC, [literal(vec(25)), 50, CFG_V1])
        self.assertEqual(self.cur.fetchall(), v2)  # same vectors, same ranking
        self.as_app()
        self.cur.execute(SEMANTIC_V2, [literal(vec(25)), 50, CFG_V2])
        self.assertEqual(self.cur.fetchall(), v2)
        self.cur.execute("reset role")

        self.assertEqual(self.sqlstate(SEMANTIC_V2, [literal(vec(0)), 51, CFG_V2]), "KBF01")
        self.assertEqual(self.sqlstate(SEMANTIC_V2, [literal(vec(0)), 50, "c" * 64]), "KBF02")
        self.assertEqual(self.sqlstate(SEMANTIC_V2, [literal([0.0] * DIMS), 50, CFG_V2]), "KBF03")

    def test_semantic_v2_refuses_configs_not_article_v2(self):
        self.cur.execute(self.v2)
        # The v1 config is complete and fresh for v1, and still refused here.
        self.assertEqual(self.sqlstate(SEMANTIC_V2, [literal(vec(0)), 50, CFG_V1]), "KBF02")
        # Fully loaded with article-v2 hashes, but labeled anything but 'article-v2'.
        for i, label in enumerate(("article-v1", "article-v2 ", "Article-v2", "article-v3")):
            cfg = f"{i + 1:x}" * 64
            with self.subTest(template_version=label):
                self.load_v2_config(cfg, label)
                self.assertEqual(self.sqlstate(SEMANTIC_V2, [literal(vec(0)), 50, cfg]), "KBF02")
        # v1 search is unaffected by the article-v2 check.
        self.cur.execute(SEMANTIC, [literal(vec(0)), 50, CFG_V1])
        self.assertEqual(len(self.cur.fetchall()), len(ARTICLES))

    def test_semantic_v2_stale_and_incomplete(self):
        self.cur.execute(self.v2)
        self.load_v2_config()
        # A v1 hash on an article with staff text is stale under article-v2.
        title, text, _, _ = ARTICLES["2"]
        self.cur.execute("update article_embeddings set source_text_sha256 = %s "
                         "where config_sha256 = %s and source_article_id = '2'",
                         [article_v1_sha256(title, text), CFG_V2])
        self.assertEqual(self.sqlstate(SEMANTIC_V2, [literal(vec(0)), 50, CFG_V2]), "KBF05")
        self.cur.execute("delete from article_embeddings "
                         "where config_sha256 = %s and source_article_id = '2'", [CFG_V2])
        self.assertEqual(self.sqlstate(SEMANTIC_V2, [literal(vec(0)), 50, CFG_V2]), "KBF04")
        # Without staff text, the v1 and v2 hashes agree: article 1 is not stale.
        title, text, _, _ = ARTICLES["1"]
        self.assertEqual(article_v2_sha256(title, text, None), article_v1_sha256(title, text))


if __name__ == "__main__":
    unittest.main()
