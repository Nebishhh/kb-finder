"""Live DB tests for the semantic embeddings migration and kb_search_semantic.
Skipped unless KB_FINDER_TEST_DATABASE_URL is set (except the offline pin check).

Isolation follows test_search_sql.py: one transaction per test, always rolled back.
Inside it a uniquely named throwaway schema is created, search_path is set to ONLY
that schema, and both migration files are run. `extensions` is deliberately not on
search_path, so any unqualified pgvector reference in the migration fails here.
Vectors are synthetic: unit vectors in the first two coordinates, so the expected
cosine similarity of each article is known exactly.
"""
import hashlib
import math
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

URL = os.environ.get("KB_FINDER_TEST_DATABASE_URL")
MIGRATION_DIR = ROOT / "supabase" / "migrations"
ARTICLES_MIGRATION = MIGRATION_DIR / "20260930042716_create_articles.sql"
ARTICLES_MIGRATION_SHA256 = "93903628588f6f313e33ebe130d6c0de8f0e17817602034c27289f46a7d02d59"
SEMANTIC_MIGRATIONS = sorted(MIGRATION_DIR.glob("*_add_semantic_embeddings.sql"))

DIMS = 1536
CFG = "a" * 64
CFG_B = "b" * 64
SEARCH = ("select rank, source_article_id, title, url, category, score "
          "from kb_search_semantic(%s::text::extensions.vector, %s::int, %s::text)")

if URL:
    import psycopg
    from psycopg import sql


def vec(angle_deg=0.0, dims=DIMS):
    """Unit vector at angle_deg in the plane of the first two coordinates."""
    v = [0.0] * dims
    v[0] = math.cos(math.radians(angle_deg))
    if dims > 1:
        v[1] = math.sin(math.radians(angle_deg))
    return v


def literal(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def article_v1_sha256(title, full_text):
    return hashlib.sha256(f"Title: {title}\n\n{full_text}".encode("utf-8")).hexdigest()


class PinnedMigrationTest(unittest.TestCase):
    """Offline: the frozen lexical migration is unchanged and the new one exists once."""

    def test_articles_migration_unchanged(self):
        digest = hashlib.sha256(ARTICLES_MIGRATION.read_bytes()).hexdigest()
        self.assertEqual(digest, ARTICLES_MIGRATION_SHA256)

    def test_one_semantic_migration_sorting_after_articles(self):
        self.assertEqual(len(SEMANTIC_MIGRATIONS), 1, SEMANTIC_MIGRATIONS)
        self.assertGreater(SEMANTIC_MIGRATIONS[0].name, ARTICLES_MIGRATION.name)
        # test_search_sql.py takes the single *_create_articles.sql match.
        self.assertFalse(SEMANTIC_MIGRATIONS[0].name.endswith("_create_articles.sql"))


def public_state(conn):
    """Catalog view of public plus row counts of the tables these tests could touch."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.article_embeddings')::oid, "
                    "to_regclass('public.embedding_configs')::oid")
        oids = cur.fetchone()
        counts = []
        for name in ("article_embeddings", "embedding_configs"):
            cur.execute("select to_regclass(%s) is not null", [f"public.{name}"])
            if cur.fetchone()[0]:
                cur.execute(sql.SQL("select count(*) from {}").format(
                    sql.Identifier("public", name)))
                counts.append(cur.fetchone()[0])
            else:
                counts.append(None)
    return base.public_snapshot(conn), base.public_article_count(conn), oids, counts


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class SemanticSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert len(SEMANTIC_MIGRATIONS) == 1, SEMANTIC_MIGRATIONS
        cls.migrations = [ARTICLES_MIGRATION.read_text(encoding="utf-8"),
                          SEMANTIC_MIGRATIONS[0].read_text(encoding="utf-8")]
        with psycopg.connect(URL, autocommit=True) as c:
            cls.public_before = public_state(c)

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(URL, autocommit=True) as c:
            assert public_state(c) == cls.public_before, "public objects or rows changed"

    def setUp(self):
        self.schema = f"kbtest_{uuid.uuid4().hex}"
        self.conn = psycopg.connect(URL)  # autocommit off: one transaction
        self.addCleanup(self.rollback_and_verify)
        cur = self.conn.cursor()
        cur.execute("select exists (select 1 from pg_namespace where nspname = %s)", [self.schema])
        if cur.fetchone()[0]:
            self.fail(f"schema {self.schema} already exists; refusing to run")
        cur.execute(sql.SQL("create schema {}").format(sql.Identifier(self.schema)))
        cur.execute(sql.SQL("set local search_path to {}").format(sql.Identifier(self.schema)))
        for name in ("articles", "article_embeddings", "embedding_configs"):
            try:
                with self.conn.transaction():
                    cur.execute(sql.SQL("select 1 from {} limit 1").format(sql.Identifier(name)))
                leaked = True
            except psycopg.errors.UndefinedTable:
                leaked = False
            if leaked:
                self.fail(f"unqualified '{name}' resolved outside the throwaway schema")
        for migration in self.migrations:
            cur.execute(migration)
        cur.close()

    def rollback_and_verify(self):
        self.conn.rollback()
        self.conn.close()
        with psycopg.connect(URL, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute("select exists (select 1 from pg_namespace where nspname = %s)",
                            [self.schema])
                self.assertFalse(cur.fetchone()[0], f"{self.schema} survived rollback")

    # -------------------------------------------------------------- helpers

    def load(self, *records, prune=False):
        return ingest_kb.sync(self.conn, base.rows(*records), prune)

    def add_config(self, cfg=CFG):
        with self.conn.cursor() as cur:
            cur.execute("insert into embedding_configs (config_sha256, config, model, dimensions, "
                        "template_version) values (%s, '{}'::jsonb, "
                        "'text-embedding-3-small', 1536, 'article-v1')", [cfg])

    def embed(self, rec, v, cfg=CFG, source_sha=None, kind="text", tokens=10):
        sha = source_sha or article_v1_sha256(rec["title"], rec["fullText"])
        with self.conn.cursor() as cur:
            cur.execute(
                "insert into article_embeddings (config_sha256, source_article_id, "
                "source_text_sha256, input_kind, input_sha256, input_tokens, truncated, embedding) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s::text::extensions.vector)",
                [cfg, rec["sourceArticleId"], sha, kind, "c" * 64, tokens, kind == "tokens",
                 literal(v)])

    def search(self, q=None, k=50, cfg=CFG, q_literal=None):
        with self.conn.cursor() as cur:
            cur.execute(SEARCH, [q_literal if q_literal is not None else literal(q or vec(0)),
                                 k, cfg])
            return cur.fetchall()

    def ids(self, **kw):
        return [r[1] for r in self.search(**kw)]

    def assert_sqlstate(self, code, fn, *args, **kw):
        with self.assertRaises(psycopg.Error) as ctx:
            with self.conn.transaction():
                fn(*args, **kw)
        self.assertEqual(ctx.exception.sqlstate, code)
        return ctx.exception

    def corpus(self, *angles):
        """One article per angle, IDs 1..n, all embedded under CFG."""
        self.add_config()
        recs = [base.article(str(i), title=f"Heading {i}") for i in range(1, len(angles) + 1)]
        self.load(*recs)
        for rec, angle in zip(recs, angles):
            self.embed(rec, vec(angle))
        return recs

    # -------------------------------------------------------------- schema

    def test_objects_live_in_throwaway_schema_and_extensions(self):
        with self.conn.cursor() as cur:
            cur.execute("select n.nspname from pg_extension e join pg_namespace n "
                        "on n.oid = e.extnamespace where e.extname = 'vector'")
            self.assertEqual(cur.fetchone()[0], "extensions")
            for name in ("article_embeddings", "embedding_configs"):
                cur.execute("select n.nspname from pg_class c join pg_namespace n "
                            "on n.oid = c.relnamespace where c.oid = %s::regclass", [name])
                self.assertEqual(cur.fetchone()[0], self.schema)
            cur.execute("select format_type(atttypid, atttypmod) from pg_attribute "
                        "where attrelid = 'article_embeddings'::regclass and attname = 'embedding'")
            self.assertEqual(cur.fetchone()[0], "extensions.vector(1536)")
            cur.execute("select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                        "where n.nspname = %s and p.proname like 'kb\\_%%'", [self.schema])
            self.assertEqual(cur.fetchone()[0], 5)

    def test_no_vector_index(self):
        with self.conn.cursor() as cur:
            cur.execute("select count(*) from pg_index i join pg_class c on c.oid = i.indexrelid "
                        "join pg_am am on am.oid = c.relam "
                        "where i.indrelid = 'article_embeddings'::regclass "
                        "and am.amname in ('hnsw', 'ivfflat')")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_sql_text_hash_matches_python(self):
        texts = [("Plain", "Body."), ("naïve café", "日本語 テスト 😀"),
                 ("Tabs\tand\nlines", "line1\r\nline2\n\nend"), ("Quote's \\", "$$ %s ''")]
        with self.conn.cursor() as cur:
            for title, full in texts:
                with self.subTest(title=title):
                    cur.execute("select kb_article_v1_sha256(%s, %s)", [title, full])
                    self.assertEqual(cur.fetchone()[0], article_v1_sha256(title, full))

    # -------------------------------------------------------------- ranking

    def test_orders_by_cosine_similarity(self):
        self.corpus(60, 0, 30, 90)
        got = self.search(vec(0))
        self.assertEqual([r[1] for r in got], ["2", "3", "1", "4"])
        self.assertEqual([r[0] for r in got], [1, 2, 3, 4])
        for r, angle in zip(got, (0, 30, 60, 90)):
            self.assertAlmostEqual(r[5], math.cos(math.radians(angle)), places=6)

    def test_score_is_finite_python_float(self):
        self.corpus(0, 45)
        for r in self.search(vec(10)):
            self.assertIs(type(r[5]), float)
            self.assertTrue(math.isfinite(r[5]))

    def test_opposite_vector_scores_minus_one(self):
        self.corpus(180)
        self.assertAlmostEqual(self.search(vec(0))[0][5], -1.0, places=6)

    def test_ties_break_on_source_article_id_collate_c(self):
        self.add_config()
        recs = [base.article(s) for s in ("9", "10", "100")]
        self.load(*recs)
        for rec in recs:
            self.embed(rec, vec(20))
        got = self.search(vec(0))
        self.assertEqual([r[1] for r in got], ["10", "100", "9"])
        self.assertEqual([r[0] for r in got], [1, 2, 3])
        self.assertEqual(len({r[5] for r in got}), 1)

    def test_k_limits_rows(self):
        self.corpus(0, 10, 20, 30, 40)
        self.assertEqual(self.ids(k=2), ["1", "2"])
        self.assertEqual(len(self.search(k=50)), 5)
        self.assertEqual(len(self.search(k=1)), 1)

    def test_returns_title_url_category(self):
        recs = self.corpus(0)
        rank, sid, title, url, category, _ = self.search()[0]
        self.assertEqual((rank, sid, title, url, category),
                         (1, "1", recs[0]["title"], recs[0]["url"], None))

    def test_empty_corpus_with_loaded_config_returns_nothing(self):
        self.add_config()
        self.assertEqual(self.search(), [])

    def test_only_the_requested_config_is_searched(self):
        recs = self.corpus(0, 50)
        self.add_config(CFG_B)
        self.embed(recs[0], vec(50), cfg=CFG_B)
        self.embed(recs[1], vec(0), cfg=CFG_B)
        self.assertEqual(self.ids(cfg=CFG), ["1", "2"])
        self.assertEqual(self.ids(cfg=CFG_B), ["2", "1"])

    def test_query_literal_forms(self):
        self.corpus(0)
        small = vec(0)
        small[2] = 1e-7  # repr gives exponent notation
        self.assertEqual(len(self.search(small)), 1)
        for bad in ("[NaN" + ",0" * (DIMS - 1) + "]", "[Infinity" + ",0" * (DIMS - 1) + "]",
                    "not a vector", "[]"):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(psycopg.Error):
                    with self.conn.transaction():
                        self.search(q_literal=bad)

    def test_staff_only_tags_description_do_not_affect_ranking_or_staleness(self):
        self.add_config()
        rec = base.article("1", title="Heading", full_text="Body.")
        self.load(rec)
        self.embed(rec, vec(0))
        changed = base.article("1", title="Heading", full_text="Body.", tags=["x"],
                               description="New description", staff_text="Internal steps")
        self.assertEqual(self.load(changed)["updated"], 1)
        self.assertEqual(self.ids(), ["1"])

    # -------------------------------------------------------------- loud failures

    def test_kbf01_k_out_of_range(self):
        self.corpus(0)
        for k in (0, -1, 51, None):
            with self.subTest(k=k):
                self.assert_sqlstate("KBF01", self.search, k=k)

    def test_kbf02_unknown_or_null_config(self):
        self.corpus(0)
        for cfg in (CFG_B, None, "not-a-hash"):
            with self.subTest(cfg=cfg):
                self.assert_sqlstate("KBF02", self.search, cfg=cfg)

    def test_kbf03_wrong_dimension_or_zero_query(self):
        self.corpus(0)
        self.assert_sqlstate("KBF03", self.search, q_literal="[1,0,0]")
        self.assert_sqlstate("KBF03", self.search, q_literal=literal(vec(0, dims=DIMS + 1)))
        self.assert_sqlstate("KBF03", self.search, q_literal=literal([0.0] * DIMS))

    def test_kbf04_article_without_embedding(self):
        self.corpus(0, 10)
        self.load(base.article("1", title="Heading 1"), base.article("2", title="Heading 2"),
                  base.article("3"))
        self.assert_sqlstate("KBF04", self.search)

    def test_kbf04_other_config_does_not_count(self):
        recs = self.corpus(0, 10)
        self.add_config(CFG_B)
        self.embed(recs[0], vec(0), cfg=CFG_B)
        self.assert_sqlstate("KBF04", self.search, cfg=CFG_B)
        self.assertEqual(len(self.search(cfg=CFG)), 2)

    def test_kbf05_changed_title_or_text(self):
        for field in ("title", "full_text"):
            with self.subTest(field=field):
                with self.conn.transaction(force_rollback=True):  # fresh state per field
                    self.corpus(0, 10)
                    kw = ({"title": "Edited heading"} if field == "title"
                          else {"title": "Heading 2", "full_text": "Edited body."})
                    self.assertEqual(self.load(base.article("1", title="Heading 1"),
                                               base.article("2", **kw))["updated"], 1)
                    self.assert_sqlstate("KBF05", self.search)

    def test_kbf05_wrong_stored_hash(self):
        self.add_config()
        rec = base.article("1")
        self.load(rec)
        self.embed(rec, vec(0), source_sha="d" * 64)
        self.assert_sqlstate("KBF05", self.search)

    # -------------------------------------------------------------- integrity

    def test_prune_of_embedded_article_is_refused(self):
        self.corpus(0, 10)
        exc = self.assert_sqlstate("23503", self.load, base.article("1", title="Heading 1"),
                                   prune=True)
        self.assertEqual(exc.diag.constraint_name, "article_embeddings_source_article_id_fkey")
        self.assertEqual(self.ids(), ["1", "2"])

    def test_prune_of_unembedded_article_still_works(self):
        recs = self.corpus(0)
        self.load(recs[0], base.article("2"))
        self.assertEqual(self.load(recs[0], prune=True)["deleted"], 1)
        self.assertEqual(self.ids(), ["1"])

    def test_embedding_for_unknown_article_is_refused(self):
        self.add_config()
        self.assert_sqlstate("23503", self.embed, base.article("77"), vec(0))

    def test_embedding_for_unknown_config_is_refused(self):
        rec = base.article("1")
        self.load(rec)
        self.assert_sqlstate("23503", self.embed, rec, vec(0), cfg=CFG_B)

    def test_deleting_config_removes_its_embeddings(self):
        self.corpus(0, 10)
        with self.conn.cursor() as cur:
            cur.execute("delete from embedding_configs where config_sha256 = %s", [CFG])
            cur.execute("select count(*) from article_embeddings")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("select count(*) from articles")
            self.assertEqual(cur.fetchone()[0], 2)

    def test_column_checks(self):
        self.add_config()
        rec = base.article("1")
        self.load(rec)
        # Dimension mismatch is a data exception raised by pgvector (class 22).
        with self.assertRaises(psycopg.errors.DataException):
            with self.conn.transaction():
                self.embed(rec, vec(0, dims=DIMS - 1))
        cases = {
            "zero vector": dict(v=[0.0] * DIMS),
            "bad source hash": dict(v=vec(0), source_sha="XYZ"),
            "zero tokens": dict(v=vec(0), tokens=0),
            "too many tokens": dict(v=vec(0), tokens=8193),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.assert_sqlstate("23514", self.embed, rec, **kw)
        with self.subTest("text input marked truncated"):
            def insert_text_truncated():
                with self.conn.cursor() as cur:
                    cur.execute(
                        "insert into article_embeddings (config_sha256, source_article_id, "
                        "source_text_sha256, input_kind, input_sha256, input_tokens, truncated, "
                        "embedding) values (%s, '1', %s, 'text', %s, 5, true, "
                        "%s::text::extensions.vector)",
                        [CFG, "c" * 64, "c" * 64, literal(vec(0))])
            self.assert_sqlstate("23514", insert_text_truncated)
        for bad_cfg in ("short", "A" * 64):
            with self.subTest(cfg=bad_cfg):
                self.assert_sqlstate("23514", self.add_config, bad_cfg)

    def test_lexical_search_unaffected_by_embeddings(self):
        self.add_config()
        recs = [base.article("1", title="Zebra setup"),
                base.article("2", full_text="Guide mentions zebra once.")]
        self.load(*recs)
        with self.conn.cursor() as cur:
            cur.execute("select source_article_id from kb_search_lexical('zebra', 50)")
            before = cur.fetchall()
        for rec in recs:
            self.embed(rec, vec(0))
        with self.conn.cursor() as cur:
            cur.execute("select source_article_id from kb_search_lexical('zebra', 50)")
            self.assertEqual(cur.fetchall(), before)
        self.assertEqual([r[0] for r in before], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
