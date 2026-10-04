"""Tests for scripts/db_fingerprint.py. Offline: the database is a fake connection that
answers the script's fixed SQL with placeholder rows. One live test (counts only) is
skipped unless KB_FINDER_TEST_DATABASE_URL is set."""
import contextlib
import hashlib
import io
import os
import re
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import db_fingerprint  # noqa: E402
import embed_kb  # noqa: E402
import psycopg  # noqa: E402

LIVE_URL = os.environ.get("KB_FINDER_TEST_DATABASE_URL")
PASSWORD = "SENTINEL_PASSWORD_k2v9"
LOCAL_URL = f"postgresql://postgres:{PASSWORD}@127.0.0.1:54322/postgres"
REMOTE_URL = f"postgresql://kb_app.tenant:{PASSWORD}@db.example.test:6543/postgres"
CFG, CFG_SHA = embed_kb.load_config(embed_kb.CONFIG_PATH)


def article_row(sid, **over):
    """One row as the article query returns it, in ARTICLE_FIELDS order. Placeholder text."""
    values = {
        "source_article_id": sid,
        "title": f"Placeholder title {sid}",
        "url": f"https://td.usd.edu/TDClient/33/Portal/KB/Article/{sid}/Placeholder",
        "category": "Placeholder category",
        "tags": ["alpha", "beta"],
        "description": "Placeholder description.",
        "published_time": "2020-01-02T03:04:00.000000Z",
        "modified_time": None,
        "full_text": "Placeholder body text é.",
        "question": None,
        "answer": None,
        "issue": "Placeholder issue.",
        "environment": None,
        "cause": None,
        "resolution": "Placeholder resolution.",
        "sections": [{"heading": None, "text": "Intro."},
                     {"heading": "Issue", "text": "Placeholder issue."}],
        "staff_only": {"text": "Staff text.", "links": [{"text": "L", "href": "https://x.test/"}]},
        "audience_labels": [],
        "footer": None,
        "parser_version": "2",
        "warnings": [],
        "search_vector": "'bodi':5C 'placehold':1A,4C 'titl':2A",
        "kb_article_v1_sha256": "a" * 64,
        "source_text_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "input_kind": "text",
        "input_tokens": 120,
        "truncated": False,
    }
    unknown = set(over) - set(values)
    assert not unknown, unknown
    values.update(over)
    return tuple(values[name] for name, _ in db_fingerprint.ARTICLE_FIELDS)


def fake_db(**over):
    db = {
        "counts": {"articles": 3, "article_embeddings": 2, "embedding_configs": 1},
        "configs": [(CFG_SHA, dict(CFG), CFG["model"], CFG["dimensions"],
                     CFG["template_version"])],
        "articles": [
            article_row("10"),
            article_row("2", source_text_sha256="c" * 64, input_sha256="d" * 64,
                        input_kind="tokens", input_tokens=8000, truncated=True),
            article_row("30", source_text_sha256=None, input_sha256=None, input_kind=None,
                        input_tokens=None, truncated=None),
        ],
        "vectors": [("10", [0.1, -0.25, 1.0]), ("2", [0.5, 0.0, -1.0])],
        "pgvector": "0.8.0",
        "server_version": "17.4",
        "settings": {"shared_preload_libraries": "pg_stat_statements",
                     "statement_timeout": "5s", "log_statement": "none"},
        "hidden": set(),
        "current_user": "postgres",
    }
    db.update(over)
    return db


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        db = self.conn.db
        self.conn.statements.append((query, params))
        q = query.strip().lower()
        if db.get("fail_on") and db["fail_on"] in q:
            raise psycopg.errors.QueryCanceled("canceled")
        if q.startswith("select count(*) from "):
            self.rows = [(db["counts"][q.rsplit(" ", 1)[1]],)]
        elif q.startswith("select config_sha256, config"):
            self.rows = list(db["configs"])
        elif "kb_article_v1_sha256" in q:
            self.rows = sorted(db["articles"], key=lambda r: r[0].encode())
        elif "embedding::real[]" in q:
            self.rows = sorted(db["vectors"], key=lambda r: r[0].encode())
        elif "from pg_extension" in q:
            self.rows = [(db["pgvector"],)] if db["pgvector"] else []
        elif "current_setting('server_version')" in q:
            self.rows = [(db["server_version"],)]
        elif q.startswith("select current_setting(%s, true)"):
            if params[0] in db["hidden"]:
                raise psycopg.errors.InsufficientPrivilege("must be superuser")
            self.rows = [(db["settings"].get(params[0]),)]
        elif q == "select current_user":
            self.rows = [(db["current_user"],)]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(list(self.rows))


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.statements = []
        self.calls = []

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.calls.append("rollback")

    def commit(self):
        self.calls.append("commit")

    def close(self):
        self.calls.append("close")


class FakeConnect:
    def __init__(self, db=None, exc=None):
        self.conn = FakeConn(db or fake_db())
        self.exc = exc
        self.args = []

    def __call__(self, url, **kwargs):
        self.args.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.conn


def run_main(argv=(), url=LOCAL_URL, db=None, connect=None):
    connect = connect or FakeConnect(db)

    def runner(u, sha, settings):
        return db_fingerprint.run_fingerprint(u, sha, settings, connect=connect)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = db_fingerprint.main(list(argv), environ={"DATABASE_URL": url} if url else {},
                                   run_fingerprint=runner)
    return code, out.getvalue(), connect


def line(output, prefix):
    return next(l for l in output.splitlines() if l.startswith(prefix))


class ReadOnlyTransactionTest(unittest.TestCase):
    def test_read_only_first_and_rolled_back(self):
        code, _, connect = run_main()
        self.assertEqual(code, 0)
        self.assertEqual(connect.args[0][1], {"prepare_threshold": None})
        self.assertEqual(connect.conn.statements[0][0], "set transaction read only")
        self.assertEqual(connect.conn.calls, ["rollback", "close"])

    def test_rolled_back_on_error(self):
        connect = FakeConnect(fake_db(fail_on="kb_article_v1_sha256"))
        code, out, _ = run_main(connect=connect)
        self.assertEqual(code, 3)
        self.assertIn("rolled back", out)
        self.assertEqual(connect.conn.calls, ["rollback", "close"])

    def test_no_write_statements(self):
        _, _, connect = run_main(["--settings"])
        for query, _ in connect.conn.statements:
            q = query.strip().lower()
            self.assertTrue(q.startswith(("select", "set transaction read only", "savepoint",
                                          "release savepoint", "rollback to savepoint")), q)


class OutputTest(unittest.TestCase):
    def test_identical_data_identical_bytes(self):
        a = run_main(["--per-article", "--settings"])[1]
        b = run_main(["--per-article", "--settings"])[1]
        self.assertEqual(a.encode(), b.encode())

    def test_order_is_c_collation(self):
        out = run_main(["--per-article"])[1].splitlines()
        self.assertEqual([l.split()[0] for l in out[-3:]], ["10", "2", "30"])

    def test_one_field_changes_aggregate_and_one_line(self):
        base = run_main(["--per-article"])[1]
        changes = {
            "tags": ["alpha", "gamma"],
            "description": "Placeholder description!",
            "url": "https://td.usd.edu/TDClient/33/Portal/KB/Article/2/Other",
            "title": "Other title",
            "category": None,
            "published_time": "2020-01-02T03:04:00.000001Z",
            "full_text": "Placeholder body text e.",
            "sections": [{"heading": None, "text": "Intro."}],
            "staff_only": None,
            "audience_labels": ["USD STAFF ONLY"],
            "footer": "",
            "warnings": ["footer_nonstandard"],
            "search_vector": "'bodi':5C",
            "kb_article_v1_sha256": "f" * 64,
            "input_tokens": 7999,
            "truncated": False,
        }
        for field, value in changes.items():
            rows = fake_db()["articles"]
            fields = dict(source_text_sha256="c" * 64, input_sha256="d" * 64,
                          input_kind="tokens", input_tokens=8000, truncated=True)
            fields[field] = value
            rows[1] = article_row("2", **fields)
            out = run_main(["--per-article"], db=fake_db(articles=rows))[1]
            with self.subTest(field=field):
                self.assertNotEqual(line(base, "articles_sha256"), line(out, "articles_sha256"))
                self.assertEqual(line(base, "vectors_sha256"), line(out, "vectors_sha256"))
                diff = set(base.splitlines()) ^ set(out.splitlines())
                self.assertEqual({l.split()[0] for l in diff}, {"articles_sha256", "2"})
                old, new = line(base, "2 ").split(), line(out, "2 ").split()
                self.assertNotEqual(old[1], new[1])
                self.assertEqual(old[2], new[2])  # vector hash unchanged

    def test_jsonb_key_order_does_not_matter(self):
        base = run_main(["--per-article"])[1]
        rows = fake_db()["articles"]
        rows[0] = article_row("10", staff_only={"links": [{"href": "https://x.test/",
                                                           "text": "L"}],
                                                "text": "Staff text."})
        self.assertEqual(base, run_main(["--per-article"], db=fake_db(articles=rows))[1])

    def test_null_is_not_empty(self):
        base = line(run_main()[1], "articles_sha256")
        for field, value in (("footer", ""), ("tags", None), ("audience_labels", None)):
            rows = fake_db()["articles"]
            rows[0] = article_row("10", **{field: value})
            with self.subTest(field=field):
                self.assertNotEqual(base, line(run_main(db=fake_db(articles=rows))[1],
                                               "articles_sha256"))

    def test_fields_cover_every_articles_column_but_id(self):
        sql = (ROOT / "supabase" / "migrations" / "20260930042716_create_articles.sql").read_text()
        body = sql.split("create table articles (", 1)[1].split("\n);", 1)[0]
        columns = {m.group(1) for m in re.finditer(r"^  ([a-z_]+) ", body, re.M)}
        names = [n for n, _ in db_fingerprint.ARTICLE_FIELDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(columns - set(names), {"id"})
        self.assertIn("a.search_vector::text", db_fingerprint.ARTICLE_ROWS_SQL)
        self.assertNotIn("loaded_at", db_fingerprint.ARTICLE_ROWS_SQL)
        self.assertEqual(sum("at time zone 'UTC'" in e for _, e in db_fingerprint.ARTICLE_FIELDS), 2)

    def test_wrong_field_count_refused(self):
        with self.assertRaises(ValueError):
            db_fingerprint.article_row_bytes(("10", "x"))

    def test_aggregate_matches_definition(self):
        out = run_main()[1]
        rows = sorted(fake_db()["articles"], key=lambda r: r[0].encode())
        expected = hashlib.sha256(b"".join(embed_kb.canonical_json(list(r)) + b"\n"
                                           for r in rows)).hexdigest()
        self.assertEqual(line(out, "articles_sha256").split()[1], expected)
        self.assertIn(f"rows 3 config {CFG_SHA}", line(out, "articles_sha256"))

    def test_counts_config_and_versions(self):
        out = run_main()[1].splitlines()
        self.assertEqual(out[:4], [db_fingerprint.FORMAT_VERSION, "count articles 3",
                                   "count article_embeddings 2", "count embedding_configs 1"])
        self.assertIn(f"config_file sha256 {CFG_SHA}", out)
        self.assertIn(f"embedding_config {CFG_SHA} model text-embedding-3-small dimensions 1536 "
                      "template_version article-v1", out)
        self.assertIn(f"embedding_config {CFG_SHA} config_equals_file true "
                      "sha256_equals_file true", out)
        self.assertIn("pgvector 0.8.0", out)
        self.assertIn("server_version 17.4", out)
        self.assertFalse(any(l.startswith("setting ") for l in out))
        self.assertEqual(len(out), 11)  # no per-article lines without the flag

    def test_config_mismatch_reported(self):
        stored = dict(CFG, dimensions=1536.0)  # equal as a Python dict, not as JSON
        db = fake_db(configs=[("0" * 64, stored, CFG["model"], 1536, "article-v1")])
        out = run_main(db=db)[1]
        self.assertIn(f"embedding_config {'0' * 64} config_equals_file false "
                      "sha256_equals_file false", out)

    def test_config_sha256_is_embed_kb_canonical(self):
        raw = embed_kb.CONFIG_PATH.read_bytes()
        self.assertEqual(CFG_SHA, hashlib.sha256(embed_kb.canonical_json(CFG)).hexdigest())
        self.assertEqual(CFG_SHA, hashlib.sha256(raw).hexdigest())


class VectorHashTest(unittest.TestCase):
    def test_float32_little_endian(self):
        self.assertEqual(db_fingerprint.vector_bytes([1.0, -2.5]),
                         bytes.fromhex("0000803f000020c0"))
        # 0.1 is not exact in float32: the bytes are of the float32 value, not a double.
        self.assertEqual(db_fingerprint.vector_bytes([0.1]), struct.pack("<f", 0.1))
        self.assertEqual(len(db_fingerprint.vector_bytes([0.1])), 4)

    def test_aggregate_matches_definition(self):
        out = run_main()[1]
        h = hashlib.sha256()
        for sid, v in [("10", [0.1, -0.25, 1.0]), ("2", [0.5, 0.0, -1.0])]:
            h.update(f"{sid} 3\n".encode())
            h.update(struct.pack("<3f", *v))
        self.assertEqual(line(out, "vectors_sha256"),
                         f"vectors_sha256 {h.hexdigest()} rows 2 config {CFG_SHA} "
                         "encoding float32le")

    def test_float32_rounding_invisible_but_real_change_seen(self):
        base = line(run_main()[1], "vectors_sha256")
        # A double that rounds to the same float32 gives the same hash...
        same = fake_db(vectors=[("10", [struct.unpack("<f", struct.pack("<f", 0.1))[0],
                                        -0.25, 1.0]), ("2", [0.5, 0.0, -1.0])])
        self.assertEqual(base, line(run_main(db=same)[1], "vectors_sha256"))
        # ...and a one-ulp float32 change does not.
        bumped = struct.unpack("<f", struct.pack("<I", struct.unpack(
            "<I", struct.pack("<f", 0.1))[0] + 1))[0]
        diff = fake_db(vectors=[("10", [bumped, -0.25, 1.0]), ("2", [0.5, 0.0, -1.0])])
        self.assertNotEqual(base, line(run_main(db=diff)[1], "vectors_sha256"))

    def test_per_article_vector_hash(self):
        out = run_main(["--per-article"])[1]
        for sid, v in (("10", [0.1, -0.25, 1.0]), ("2", [0.5, 0.0, -1.0])):
            self.assertEqual(line(out, f"{sid} ").split()[2],
                             hashlib.sha256(struct.pack("<3f", *v)).hexdigest())
        self.assertEqual(line(out, "30 ").split()[2], "none")

    def test_vector_change_shows_on_that_article_line(self):
        base = run_main(["--per-article"])[1]
        out = run_main(["--per-article"], db=fake_db(
            vectors=[("10", [0.1, -0.25, 1.0]), ("2", [0.5, 0.0, -0.5])]))[1]
        self.assertNotEqual(line(base, "vectors_sha256"), line(out, "vectors_sha256"))
        self.assertEqual(line(base, "articles_sha256"), line(out, "articles_sha256"))
        diff = set(base.splitlines()) ^ set(out.splitlines())
        self.assertEqual({l.split()[0] for l in diff}, {"vectors_sha256", "2"})
        old, new = line(base, "2 ").split(), line(out, "2 ").split()
        self.assertEqual(old[:2], new[:2])
        self.assertNotEqual(old[2], new[2])

    def test_sql_casts_to_real_array(self):
        self.assertIn("embedding::real[]", db_fingerprint.VECTOR_ROWS_SQL)


class UrlRulesTest(unittest.TestCase):
    def test_remote_refused_without_allow_remote(self):
        code, out, connect = run_main(url=REMOTE_URL + "?sslmode=verify-full")
        self.assertEqual(code, 2)
        self.assertIn("--allow-remote", out)
        self.assertEqual(connect.args, [])

    def test_remote_refused_without_verify_full(self):
        for suffix in ("", "?sslmode=require", "?sslmode=verify-ca", "?sslmode=prefer"):
            with self.subTest(suffix=suffix):
                code, out, connect = run_main(["--allow-remote"], url=REMOTE_URL + suffix)
                self.assertEqual(code, 2)
                self.assertIn("sslmode=verify-full", out)
                self.assertEqual(connect.args, [])

    def test_remote_verify_full_allowed(self):
        code, _, connect = run_main(["--allow-remote"], url=REMOTE_URL + "?sslmode=verify-full")
        self.assertEqual(code, 0)
        self.assertEqual(len(connect.args), 1)

    def test_local_needs_no_sslmode(self):
        self.assertEqual(run_main()[0], 0)

    def test_missing_url(self):
        code, out, _ = run_main(url=None)
        self.assertEqual((code, out.strip()), (2, "config error: DATABASE_URL is not set"))


class SettingsTest(unittest.TestCase):
    def test_sampled_logging_and_pgaudit_parameter_settings_listed(self):
        for name in ("log_min_duration_sample", "log_statement_sample_rate",
                     "log_transaction_sample_rate", "pgaudit.log_parameter"):
            with self.subTest(name=name):
                self.assertIn(name, db_fingerprint.SETTINGS)
        self.assertEqual(len(set(db_fingerprint.SETTINGS)), len(db_fingerprint.SETTINGS))

    def test_missing_and_hidden_settings(self):
        db = fake_db(hidden={"pgaudit.log", "pgaudit.log_parameter"},
                     settings={"shared_preload_libraries": "pg_stat_statements",
                               "statement_timeout": "5s", "log_statement": "none",
                               "log_min_duration_sample": "-1",
                               "log_statement_sample_rate": "1",
                               "log_transaction_sample_rate": "0"})
        code, out, connect = run_main(["--settings"], db=db)
        self.assertEqual(code, 0)
        lines = [l for l in out.splitlines() if l.startswith("setting ")]
        self.assertEqual([l.split()[1] for l in lines],
                         list(db_fingerprint.SETTINGS) + ["current_user"])
        self.assertIn("setting shared_preload_libraries pg_stat_statements", lines)
        self.assertIn("setting statement_timeout 5s", lines)
        self.assertIn("setting auto_explain.log_min_duration unset", lines)
        self.assertIn("setting pgaudit.log hidden", lines)
        self.assertIn("setting pgaudit.log_parameter hidden", lines)
        self.assertIn("setting log_min_duration_sample -1", lines)
        self.assertIn("setting log_statement_sample_rate 1", lines)
        self.assertIn("setting log_transaction_sample_rate 0", lines)
        self.assertIn("setting current_user postgres", lines)
        stmts = [q for q, _ in connect.conn.statements]
        self.assertIn("rollback to savepoint kb_setting", stmts)
        self.assertEqual(connect.conn.calls, ["rollback", "close"])


class SecretsTest(unittest.TestCase):
    def assert_clean(self, out):
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn("db.example.test", out)
        self.assertNotIn("127.0.0.1", out)
        self.assertNotIn("postgresql://", out)

    def test_success_output(self):
        self.assert_clean(run_main(["--per-article", "--settings"])[1])

    def test_refusals(self):
        for argv, url in (([], REMOTE_URL), (["--allow-remote"], REMOTE_URL),
                          ([], "not a url " + PASSWORD)):
            with self.subTest(argv=argv, url=url):
                code, out, _ = run_main(argv, url=url)
                self.assertEqual(code, 2)
                self.assert_clean(out)

    def test_connection_error_text_not_printed(self):
        exc = psycopg.OperationalError(
            f'connection to server at "db.example.test" failed: password {PASSWORD} '
            f"rejected for {REMOTE_URL}")
        code, out, _ = run_main(["--allow-remote"], url=REMOTE_URL + "?sslmode=verify-full",
                                connect=FakeConnect(exc=exc))
        self.assertEqual(code, 3)
        self.assertIn("database error: OperationalError", out)
        self.assert_clean(out)


@unittest.skipUnless(LIVE_URL, "KB_FINDER_TEST_DATABASE_URL not set")
class LiveCountsTest(unittest.TestCase):
    def test_local_counts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = db_fingerprint.main([], environ={"DATABASE_URL": LIVE_URL})
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0, lines[:2])
        self.assertIn("count articles 3109", lines)
        self.assertIn("count article_embeddings 3109", lines)
        self.assertIn("count embedding_configs 1", lines)


if __name__ == "__main__":
    unittest.main()
