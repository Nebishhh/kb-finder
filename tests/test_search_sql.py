"""Live DB tests for the articles migration, kb_lexical_tsquery, kb_search_lexical and
ingest_kb.sync. Skipped unless KB_FINDER_TEST_DATABASE_URL is set.

Isolation: each test opens one transaction that is always rolled back. Inside it the
test creates a uniquely named throwaway schema, sets search_path to ONLY that schema
(SET LOCAL), checks that unqualified `articles` does not resolve, and only then runs
the actual migration file. Rows are placeholder articles written via ingest_kb.sync
on that connection. public.articles is read only for the before/after safety count.
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ingest_kb  # noqa: E402

URL = os.environ.get("KB_FINDER_TEST_DATABASE_URL")
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*_create_articles.sql"))

if URL:
    import psycopg
    from psycopg import sql


def article(sid, title="Neutral heading", full_text="Neutral body words.", tags=(),
            description=None, staff_text=None):
    rec = {
        "sourceArticleId": sid,
        "title": title,
        "url": f"https://td.usd.edu/TDClient/33/Portal/KB/Article/{sid}/Placeholder",
        "category": None,
        "tags": list(tags),
        "description": description,
        "publishedTime": "2020-01-02T03:04Z",
        "modifiedTime": None,
        "fullText": full_text,
        "question": None,
        "answer": None,
        "issue": None,
        "environment": None,
        "cause": None,
        "resolution": None,
        "sections": [{"heading": None, "text": full_text}],
        "staffOnly": None if staff_text is None else {"text": staff_text, "links": []},
        "audienceLabels": [],
        "footer": None,
        "parserVersion": ingest_kb.PARSER_VERSION,
        "warnings": [],
    }
    assert ingest_kb.validate_record(rec) == [], rec
    return rec


def rows(*records):
    return [ingest_kb.to_row(r) for r in sorted(records, key=lambda r: int(r["sourceArticleId"]))]


def public_snapshot(conn):
    """Catalog-only view of public: articles OID and kb_* function OIDs. No table reads."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.articles')::oid")
        table = cur.fetchone()[0]
        cur.execute("select coalesce(array_agg(p.oid order by p.oid), '{}') from pg_proc p "
                    "join pg_namespace n on n.oid = p.pronamespace "
                    "where n.nspname = 'public' and p.proname like 'kb\\_%'")
        funcs = cur.fetchone()[0]
    return table, funcs


def public_article_count(conn):
    """The only read of public.articles: a count, and only if the table exists."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.articles') is not null")
        if not cur.fetchone()[0]:
            return None
        cur.execute("select count(*) from public.articles")
        return cur.fetchone()[0]


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class SearchSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert len(MIGRATIONS) == 1, MIGRATIONS
        cls.migration = MIGRATIONS[0].read_text(encoding="utf-8")
        with psycopg.connect(URL, autocommit=True) as c:
            cls.public_count_before = public_article_count(c)
            cls.public_snapshot_before = public_snapshot(c)

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(URL, autocommit=True) as c:
            after = public_article_count(c)
            snap = public_snapshot(c)
        if cls.public_count_before is None:
            assert after is None, "public.articles appeared during the tests"
        else:
            assert after == cls.public_count_before, (
                f"public.articles count changed: {cls.public_count_before} -> {after}")
        assert snap == cls.public_snapshot_before, "public articles/kb_* objects changed"

    def setUp(self):
        self.schema = f"kbtest_{uuid.uuid4().hex}"
        self.conn = psycopg.connect(URL)  # autocommit off: everything below is one transaction
        self.addCleanup(self.rollback_and_verify)  # runs even if setUp fails below
        cur = self.conn.cursor()
        cur.execute("select exists (select 1 from pg_namespace where nspname = %s)", [self.schema])
        if cur.fetchone()[0]:
            self.fail(f"schema {self.schema} already exists; refusing to run")
        cur.execute(sql.SQL("create schema {}").format(sql.Identifier(self.schema)))
        cur.execute(sql.SQL("set local search_path to {}").format(sql.Identifier(self.schema)))
        cur.execute("select current_schema()")
        self.assertEqual(cur.fetchone()[0], self.schema)

        # Guard: unqualified names must not reach public before the migration runs.
        try:
            with self.conn.transaction():  # savepoint; the error rolls back only this
                cur.execute("select count(*) from articles")
                leaked = cur.fetchone()
        except psycopg.errors.UndefinedTable:
            leaked = None
        if leaked is not None:
            self.fail("unqualified 'articles' resolved outside the throwaway schema")

        cur.execute(self.migration)
        cur.execute("select n.nspname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                    "where c.oid = 'articles'::regclass")
        self.assertEqual(cur.fetchone()[0], self.schema)
        cur.execute("select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                    "where n.nspname = %s and p.proname like 'kb\\_%%'", [self.schema])
        self.assertEqual(cur.fetchone()[0], 3)
        cur.close()

    def rollback_and_verify(self):
        self.conn.rollback()
        self.conn.close()
        with psycopg.connect(URL, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute("select exists (select 1 from pg_namespace where nspname = %s)",
                            [self.schema])
                self.assertFalse(cur.fetchone()[0], f"{self.schema} survived rollback")
            self.assertEqual(public_snapshot(c), self.public_snapshot_before)

    # -------------------------------------------------------------- helpers

    def load(self, *records, prune=False):
        return ingest_kb.sync(self.conn, rows(*records), prune)

    def search(self, q):
        with self.conn.cursor() as cur:
            cur.execute("select rank, source_article_id, score from kb_search_lexical(%s, 50)", [q])
            return cur.fetchall()

    def ids(self, q):
        return [r[1] for r in self.search(q)]

    def tsquery_is_null(self, q):
        with self.conn.cursor() as cur:
            cur.execute("select kb_lexical_tsquery(%s) is null", [q])
            return cur.fetchone()[0]

    # -------------------------------------------------------------- ranking

    def test_title_match_outranks_full_text_only_match(self):
        self.load(article("1", full_text="Guide mentions zebra once."),
                  article("2", title="Zebra setup"))
        self.assertEqual(self.ids("zebra"), ["2", "1"])

    def test_staff_only_text_never_matches(self):
        self.load(article("1", staff_text="Quokka internal steps."))
        self.assertEqual(self.search("quokka"), [])

    def test_or_semantics_long_query_matches_one_term(self):
        self.load(article("1", full_text="Narwhal troubleshooting."),
                  article("2", full_text="Unrelated content."))
        self.assertEqual(self.ids("my narwhal keeps freezing whenever I open spreadsheets"), ["1"])

    def test_no_searchable_terms_give_null_and_no_results(self):
        self.load(article("1", title="The and of", full_text="The and of it."))
        for q in ("", "   ", "???", "the and of"):
            with self.subTest(q=q):
                self.assertTrue(self.tsquery_is_null(q))
                self.assertEqual(self.search(q), [])

    def test_special_characters_do_not_error(self):
        self.load(article("1", full_text="Reset e-mail on Wi-Fi. Path C:\\Users\\me\\file.txt."))
        queries = ["it's broken", "O'Brien's laptop", "back\\slash", "\\", "'", "''", "a & b",
                   "a | b", "!a", "a:b", "a:*", "(a) (b", "a <-> b", "e-mail", "wi-fi",
                   "C:\\Users\\me\\file.txt", "\\\\server\\share", "naïve café résumé",
                   "日本語 テスト", "emoji 😀 test", "--", ";", "$$ $1", "%s %%"]
        for q in queries:
            with self.subTest(q=q):
                self.search(q)  # must not raise
        self.assertEqual(self.ids("e-mail"), ["1"])

    def test_ties_break_on_source_article_id_collate_c(self):
        same = dict(title="Printer jam", full_text="Printer jam steps.")
        self.load(article("9", **same), article("10", **same), article("100", **same))
        got = self.search("printer jam")
        self.assertEqual([r[1] for r in got], ["10", "100", "9"])
        self.assertEqual([r[0] for r in got], [1, 2, 3])
        self.assertEqual(len({r[2] for r in got}), 1)

    # -------------------------------------------------------------- ingest

    def test_ingest_idempotent(self):
        recs = [article("1"), article("2"), article("3")]
        self.assertEqual(self.load(*recs), {"inserted": 3, "updated": 0, "unchanged": 0,
                                             "db_only": 0, "deleted": 0})
        self.assertEqual(self.load(*recs), {"inserted": 0, "updated": 0, "unchanged": 3,
                                             "db_only": 0, "deleted": 0})
        recs[1] = article("2", title="Changed heading")
        self.assertEqual(self.load(*recs), {"inserted": 0, "updated": 1, "unchanged": 2,
                                             "db_only": 0, "deleted": 0})

    def test_prune_off_keeps_db_only_rows(self):
        self.load(article("1"), article("2"), article("3"))
        c = self.load(article("1"), article("3"))
        self.assertEqual((c["db_only"], c["deleted"]), (1, 0))
        self.assertEqual(self.article_ids(), ["1", "2", "3"])

    def test_prune_on_deletes_exactly_missing_rows(self):
        self.load(article("1"), article("2"), article("3"), article("4"))
        c = self.load(article("1"), article("3"), prune=True)
        self.assertEqual((c["db_only"], c["deleted"]), (0, 2))
        self.assertEqual(self.article_ids(), ["1", "3"])

    def article_ids(self):
        with self.conn.cursor() as cur:
            cur.execute("select source_article_id from articles order by source_article_id::int")
            return [r[0] for r in cur.fetchall()]


if __name__ == "__main__":
    unittest.main()
