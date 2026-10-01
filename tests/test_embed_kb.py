"""Offline tests for scripts/embed_kb.py, plus live-DB tests that are skipped unless
KB_FINDER_TEST_DATABASE_URL is set.

No real transport is ever built: every test runs with socket connect, the requests
HTTP adapter and embed_kb.make_transport patched to raise, and with a fake
OPENAI_API_KEY in the environ it passes, to prove the key is never used or printed.
make_transport itself is tested against a patched requests.Session.post. The
tokenizer is a fake that emits one token per UTF-8 byte, so a cut can split a
multi-byte character. One test uses the real cl100k_base file only if it is already
in cache/tiktoken/ (never downloaded by tests).
"""
import base64
import contextlib
import hashlib
import io
import json
import math
import os
import struct
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import embed_kb  # noqa: E402
import ingest_kb  # noqa: E402
import test_search_sql as base  # noqa: E402  (helpers only)

REAL_MAKE_TRANSPORT = embed_kb.make_transport
URL = os.environ.get("KB_FINDER_TEST_DATABASE_URL")
FAKE_KEY = "sk-FAKE-KEY-never-used-3k9"
BODY_SENTINEL = "BODYSENTINEL_q7x"
DIMS = 1536
MAX = 60  # test truncation limit; short fixture articles stay below it
PINNED_ENCODING = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

if URL:
    import psycopg
    from psycopg import sql


# ------------------------------------------------------------------ fakes


class FakeEncoding:
    """One token per UTF-8 byte (byte-level, like cl100k_base, but trivially known)."""

    def encode(self, text, allowed_special=None, disallowed_special=None):
        assert allowed_special == set() and disallowed_special == (), "special-token policy"
        return list(text.encode("utf-8"))


def fake_loader(_cache_dir, _pinned):
    return FakeEncoding()


def unit_b64(index, dims=DIMS, sign=1.0):
    v = [0.0] * dims
    v[index % dims] = sign
    return base64.b64encode(struct.pack(f"<{dims}f", *v)).decode("ascii")


def fake_vector_b64(payload):
    h = hashlib.sha256(json.dumps(payload).encode()).digest()
    return unit_b64(int.from_bytes(h[:4], "big"))


def payload_tokens(payload):
    return len(payload) if isinstance(payload, list) else len(payload.encode("utf-8"))


class FakeTransport:
    def __init__(self, fail_on=None, code="OPENAI_HTTP_5XX", mutate=None, usage=None):
        self.bodies = []
        self.fail_on = fail_on
        self.code = code
        self.mutate = mutate
        self.usage = usage

    def __call__(self, body):
        self.bodies.append(body)
        if self.fail_on == len(self.bodies):
            raise embed_kb.TransportError(self.code)
        resp = {
            "object": "list",
            "model": body["model"],
            "data": [{"object": "embedding", "index": i, "embedding": fake_vector_b64(p)}
                     for i, p in enumerate(body["input"])],
            "usage": {"prompt_tokens": 0,
                      "total_tokens": self.usage if self.usage is not None
                      else sum(payload_tokens(p) for p in body["input"])},
        }
        return self.mutate(resp) if self.mutate else resp

    def inputs(self):
        return [p for b in self.bodies for p in b["input"]]


def config_bytes(max_tokens=MAX, rule="prefix-v1", **overrides):
    cfg = dict(embed_kb.FIXED_CONFIG, encoding_file_sha256=PINNED_ENCODING,
               truncation={"max_tokens": max_tokens, "rule": rule})
    cfg.update(overrides)
    return embed_kb.canonical_json(cfg)


def rec(sid, title=None, full_text=None):
    return base.article(sid, title=title or f"Heading {sid}",
                        full_text=full_text or f"Short body {sid}.",
                        tags=["TAGSENTINEL"], description="DESCSENTINEL",
                        staff_text="STAFFSENTINEL")


# The long article: 2-byte "é" sits across the MAX-byte cut (the "Title: L" prefix
# plus the blank line is 10 bytes, so "é" occupies bytes MAX-1 and MAX).
LONG_TEXT = "x" * (MAX - 11) + "é" + f" {BODY_SENTINEL} and more words after the cut."


def corpus(*sids):
    out = []
    for s in sids:
        out.append(rec(s, title="L", full_text=LONG_TEXT) if s == "7" else
                   rec(s, full_text=f"Body {s} {BODY_SENTINEL}."))
    return out


# ------------------------------------------------------------------ base


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cache_dir = self.dir / "cache"
        self.input = self.dir / "articles.jsonl"
        self.config = self.dir / "article-v1.json"
        self.config.write_bytes(config_bytes())
        self.environ = {"OPENAI_API_KEY": FAKE_KEY,
                        "DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/db"}
        for target in ("socket.socket.connect", "requests.adapters.HTTPAdapter.send"):
            p = mock.patch(target, side_effect=AssertionError(f"network: {target}"))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(embed_kb, "make_transport",
                              side_effect=AssertionError("real transport constructed"))
        p.start()
        self.addCleanup(p.stop)
        self.factory_calls = 0
        self.outputs = []

    def write_input(self, records):
        self.input.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def factory(self, transport):
        def make(environ):
            self.factory_calls += 1
            self.assertIs(environ, self.environ)
            return transport
        return make

    def run_main(self, *args, transport=None, run_load=None):
        argv = ["--input", str(self.input), "--config", str(self.config),
                "--cache-dir", str(self.cache_dir), "--tiktoken-dir", str(self.dir / "tk"),
                *args]
        kwargs = {"environ": self.environ, "encoding_loader": fake_loader}
        if transport is not None:
            kwargs["transport_factory"] = self.factory(transport)
        else:
            kwargs["transport_factory"] = lambda env: self.fail("transport factory called")
        if run_load is not None:
            kwargs["run_load"] = run_load
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = embed_kb.main(argv, **kwargs)
        text = out.getvalue()
        self.outputs.append(text)
        for secret in (FAKE_KEY, BODY_SENTINEL, "TAGSENTINEL", "DESCSENTINEL", "STAFFSENTINEL"):
            self.assertNotIn(secret, text)
        return code, text

    def embed(self, transport, cost="0.10", *extra):
        return self.run_main("embed", "--paid", "--max-cost-usd", cost, *extra,
                             transport=transport)

    def cfg(self):
        return embed_kb.load_config(self.config)

    def cache_path(self):
        return embed_kb.cache_path_for(self.cache_dir, self.cfg()[1])

    def journal_path(self):
        p = self.cache_path()
        return p.with_name(p.name + ".journal")


# ------------------------------------------------------------------ template and input


class InputTests(unittest.TestCase):
    def test_article_v1_is_exactly_title_and_full_text(self):
        r = rec("5", title="Wi-Fi help", full_text="Line one\nLine two")
        self.assertEqual(embed_kb.article_v1_text(r), "Title: Wi-Fi help\n\nLine one\nLine two")
        for absent in ("TAGSENTINEL", "DESCSENTINEL", "STAFFSENTINEL", r["url"], "\r"):
            self.assertNotIn(absent, embed_kb.article_v1_text(r))

    def test_fits_exactly_at_limit_is_text(self):
        text_len = len("Title: L\n\n".encode())
        r = rec("1", title="L", full_text="y" * (MAX - text_len))
        p = embed_kb.prepare_input(r, FakeEncoding(), {"max_tokens": MAX, "rule": "prefix-v1"})
        self.assertEqual((p["input_kind"], p["input_tokens"], p["truncated"]), ("text", MAX, False))
        self.assertEqual(p["payload"], embed_kb.article_v1_text(r))
        self.assertEqual(p["input_sha256"], hashlib.sha256(
            b"text\n" + embed_kb.article_v1_text(r).encode()).hexdigest())

    def test_one_over_limit_is_token_prefix(self):
        text_len = len("Title: L\n\n".encode())
        r = rec("1", title="L", full_text="y" * (MAX + 1 - text_len))
        p = embed_kb.prepare_input(r, FakeEncoding(), {"max_tokens": MAX, "rule": "prefix-v1"})
        self.assertEqual((p["input_kind"], p["input_tokens"], p["truncated"], p["full_tokens"]),
                         ("tokens", MAX, True, MAX + 1))
        self.assertEqual(p["payload"], list(embed_kb.article_v1_text(r).encode())[:MAX])

    def test_cut_inside_multibyte_character_keeps_exact_ids(self):
        r = rec("7", title="L", full_text=LONG_TEXT)
        text = embed_kb.article_v1_text(r)
        raw = text.encode("utf-8")
        self.assertEqual(raw[MAX - 1:MAX + 1], "é".encode())  # the cut at MAX splits the character
        p = embed_kb.prepare_input(r, FakeEncoding(), {"max_tokens": MAX, "rule": "prefix-v1"})
        self.assertEqual(bytes(p["payload"]), raw[:MAX])  # half a character, nothing replaced
        canonical = json.dumps(p["payload"], separators=(",", ":"))
        self.assertNotIn(" ", canonical)
        self.assertEqual(p["input_sha256"],
                         hashlib.sha256(b"tokens\n" + canonical.encode("ascii")).hexdigest())
        self.assertEqual(p["source_text_sha256"], hashlib.sha256(raw).hexdigest())

    def test_none_abort_rule_refuses_long_article(self):
        r = rec("7", title="L", full_text=LONG_TEXT)
        with self.assertRaises(embed_kb.EmbedError) as ctx:
            embed_kb.prepare_input(r, FakeEncoding(), {"max_tokens": MAX, "rule": "none-abort"})
        self.assertNotIn(BODY_SENTINEL, str(ctx.exception))

    def test_text_and_token_hashes_never_collide_by_kind(self):
        self.assertNotEqual(embed_kb.input_sha256("text", "[1,2]"),
                            embed_kb.input_sha256("tokens", [1, 2]))


# ------------------------------------------------------------------ config


class ConfigTests(Base):
    def test_repo_config_is_valid_and_canonical(self):
        cfg, sha = embed_kb.load_config(embed_kb.CONFIG_PATH)
        raw = embed_kb.CONFIG_PATH.read_bytes()
        self.assertEqual(sha, hashlib.sha256(raw).hexdigest())
        self.assertFalse(raw.endswith(b"\n"))
        self.assertEqual(cfg["model"], "text-embedding-3-small")
        self.assertEqual(cfg["dimensions"], 1536)
        self.assertEqual(cfg["template"], "Title: {title}\n\n{fullText}")
        self.assertEqual(cfg["encoding_file_sha256"], PINNED_ENCODING)

    def test_rejected_configs(self):
        good = json.loads(config_bytes())
        bad = {
            "pretty printed": json.dumps(good, indent=1, sort_keys=True).encode(),
            "unsorted": json.dumps(dict(reversed(list(good.items()))),
                                   separators=(",", ":")).encode(),
            "trailing newline": config_bytes() + b"\n",
            "other model": config_bytes(model="text-embedding-3-large"),
            "other dims": config_bytes(dimensions=512),
            "dims as float": config_bytes(dimensions=1536.0),
            "other template": config_bytes(template="Title: {title}\n{fullText}"),
            "unknown rule": config_bytes(rule="suffix"),
            "limit too high": config_bytes(max_tokens=8193),
            "bad encoding pin": config_bytes(encoding_file_sha256="abc"),
            "extra key": config_bytes(extra=1),
        }
        for name, raw in bad.items():
            with self.subTest(name):
                self.config.write_bytes(raw)
                self.write_input(corpus("1"))
                code, out = self.run_main("preflight")
                self.assertEqual(code, 2, out)
                self.assertTrue(out.startswith("error: config:"), out)


# ------------------------------------------------------------------ encoding file


class EncodingLoaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = embed_kb.encoding_cache_path(self.dir)
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TIKTOKEN_CACHE_DIR", None)
        p = mock.patch("socket.socket.connect", side_effect=AssertionError("network"))
        p.start()
        self.addCleanup(p.stop)

    def test_cache_file_name_matches_tiktoken(self):
        self.assertEqual(self.path.name, hashlib.sha1(embed_kb.ENCODING_URL.encode()).hexdigest())

    def test_mismatch_fails_before_tiktoken_runs(self):
        self.path.write_bytes(b"tampered")
        with mock.patch("tiktoken.get_encoding", side_effect=AssertionError("tiktoken ran")):
            with self.assertRaises(embed_kb.EmbedError) as ctx:
                embed_kb.load_encoding(self.dir, PINNED_ENCODING)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("sha256 mismatch", str(ctx.exception))

    def test_missing_without_download_fails(self):
        with mock.patch("tiktoken.get_encoding", side_effect=AssertionError("tiktoken ran")):
            with self.assertRaises(embed_kb.EmbedError):
                embed_kb.load_encoding(self.dir, PINNED_ENCODING, allow_download=False)

    def test_sets_cache_dir_itself_and_checks_after_load(self):
        content = b"pretend bpe file"
        pinned = hashlib.sha256(content).hexdigest()
        self.path.write_bytes(content)
        seen = {}

        def fake_get_encoding(name):
            seen["name"] = name
            seen["dir"] = os.environ.get("TIKTOKEN_CACHE_DIR")
            return "ENC"

        with mock.patch("tiktoken.get_encoding", side_effect=fake_get_encoding):
            self.assertEqual(embed_kb.load_encoding(self.dir, pinned), "ENC")
        self.assertEqual(seen, {"name": "cl100k_base", "dir": str(self.dir)})

    def test_downloaded_file_with_wrong_hash_fails(self):
        def fake_download(name):
            self.path.write_bytes(b"something else")
            return "ENC"

        with mock.patch("tiktoken.get_encoding", side_effect=fake_download):
            with self.assertRaises(embed_kb.EmbedError):
                embed_kb.load_encoding(self.dir, PINNED_ENCODING)

    @unittest.skipUnless(embed_kb.encoding_cache_path(embed_kb.TIKTOKEN_DIR).exists(),
                         "cl100k_base not in cache/tiktoken (tests never download it)")
    def test_real_cl100k_base(self):
        enc = embed_kb.load_encoding(embed_kb.TIKTOKEN_DIR, PINNED_ENCODING, allow_download=False)
        self.assertEqual(len(embed_kb.encode_tokens(enc, "<|endoftext|>")), 7)
        text = "Title: naïve café\n\n日本語 😀 " + "word " * 50
        tokens = embed_kb.encode_tokens(enc, text)
        self.assertEqual(enc.decode(tokens), text)
        raw = text.encode("utf-8")
        for n in range(1, len(tokens)):  # every prefix is a byte prefix of the text
            self.assertTrue(raw.startswith(enc.decode_bytes(tokens[:n])))


# ------------------------------------------------------------------ vectors


class VectorTests(unittest.TestCase):
    def test_decode_accepts_unit_vector(self):
        values = embed_kb.decode_embedding(unit_b64(3), DIMS)
        self.assertEqual(len(values), DIMS)
        self.assertEqual(values[3], 1.0)

    def test_decode_rejections(self):
        nan = [0.0] * DIMS
        nan[0] = float("nan")
        bad = {
            "not str": [0.1] * DIMS,
            "not base64": "!!!",
            "short": base64.b64encode(b"\x00" * (DIMS * 4 - 4)).decode(),
            "long": base64.b64encode(b"\x00" * (DIMS * 4 + 4)).decode(),
            "nan": base64.b64encode(struct.pack(f"<{DIMS}f", *nan)).decode(),
            "zero": base64.b64encode(b"\x00" * DIMS * 4).decode(),
            "norm 2": unit_b64(0, sign=2.0),
        }
        for name, b64 in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    embed_kb.decode_embedding(b64, DIMS)

    def test_little_endian_layout(self):
        v = [0.0] * DIMS
        v[0] = 0.6
        v[1] = 0.8
        b64 = base64.b64encode(struct.pack(f"<{DIMS}f", *v)).decode()
        got = embed_kb.decode_embedding(b64, DIMS)
        self.assertAlmostEqual(got[0], 0.6, places=6)
        self.assertAlmostEqual(got[1], 0.8, places=6)
        self.assertEqual(embed_kb.canonical_b64(got), b64)

    def test_vector_literal_round_trips_float32(self):
        values = embed_kb.decode_embedding(fake_vector_b64("x"), DIMS)
        lit = embed_kb.vector_literal(values)
        back = [float(x) for x in lit[1:-1].split(",")]
        self.assertEqual(struct.pack(f"<{DIMS}f", *back), struct.pack(f"<{DIMS}f", *values))


# ------------------------------------------------------------------ preflight


class PreflightTests(Base):
    def test_preflight_counts_and_never_builds_transport(self):
        self.write_input(corpus("1", "7", "10"))
        code, out = self.run_main("preflight")
        self.assertEqual(code, 0, out)
        lines = dict(line.split(" ", 1) for line in out.splitlines())
        self.assertIn("articles=3", lines["input"])
        self.assertIn(f"over_{MAX} 1", out)
        self.assertIn("truncated 1", out)
        self.assertIn("cache hits 0  misses 3 (no row 3, stale text 0, input changed 0)", out)
        self.assertIn("preflight only: no API call made", out)
        self.assertEqual(self.factory_calls, 0)
        self.assertFalse(self.cache_dir.exists())

    def test_default_command_is_preflight(self):
        self.write_input(corpus("1"))
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("preflight only", out)

    def test_token_stats_and_projected_cost(self):
        self.write_input(corpus("1", "7", "10"))
        preps = [embed_kb.prepare_input(r, FakeEncoding(), {"max_tokens": MAX,
                                                            "rule": "prefix-v1"})
                 for r in corpus("1", "7", "10")]
        full = sorted(p["full_tokens"] for p in preps)
        sent = sum(p["input_tokens"] for p in preps)
        code, out = self.run_main("preflight")
        self.assertIn(f"max {full[-1]} p50 {full[1]} p95 {full[2]} total {sum(full)}", out)
        self.assertIn(f"tokens to embed {sent} in 1 requests", out)
        self.assertIn(f"projected cost ${sent * 0.02 / 1e6:.6f}", out)

    def test_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(embed_kb.nearest_rank(values, 0.5), 50)
        self.assertEqual(embed_kb.nearest_rank(values, 0.95), 95)
        self.assertEqual(embed_kb.nearest_rank([7], 0.95), 7)

    def test_invalid_input_never_reaches_cache_or_transport(self):
        self.input.write_text('{"sourceArticleId": "1"}\n', encoding="utf-8")
        code, out = self.run_main("preflight")
        self.assertEqual(code, 1)
        self.assertIn("input validation failed", out)


# ------------------------------------------------------------------ cost guards


class GuardTests(Base):
    def setUp(self):
        super().setUp()
        self.write_input(corpus("1", "7", "10"))

    def test_embed_without_paid_refuses(self):
        code, out = self.run_main("embed", "--max-cost-usd", "0.10")
        self.assertEqual(code, 2)
        self.assertIn("pass --paid", out)
        self.assertEqual(self.factory_calls, 0)

    def test_max_cost_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.run_main("embed", "--paid")
        self.assertEqual(ctx.exception.code, 2)

    def test_ceiling_above_hard_cap_or_invalid(self):
        for value in ("0.51", "5", "0", "-1", "nan", "inf"):
            with self.subTest(value):
                code, out = self.run_main("embed", "--paid", "--max-cost-usd", value,
                                          transport=FakeTransport())
                self.assertEqual(code, 2, out)
        self.assertEqual(self.factory_calls, 0)

    def test_projected_cost_over_ceiling_aborts_before_any_request(self):
        t = FakeTransport()
        code, out = self.embed(t, "0.0000000001")
        self.assertEqual(code, 4)
        self.assertIn("no request sent", out)
        self.assertEqual((self.factory_calls, t.bodies), (0, []))

    def test_token_hard_cap_aborts_before_any_request(self):
        t = FakeTransport()
        with mock.patch.object(embed_kb, "HARD_CAP_TOKENS", 10):
            code, out = self.embed(t)
        self.assertEqual(code, 4)
        self.assertEqual((self.factory_calls, t.bodies), (0, []))

    def test_billed_tokens_over_ceiling_stop_mid_run(self):
        t = FakeTransport(usage=10_000_000)  # $0.20 reported for the first batch
        with mock.patch.object(embed_kb, "BATCH_MAX_INPUTS", 1):
            code, out = self.embed(t, "0.10")
        self.assertEqual(code, 4)
        self.assertEqual(len(t.bodies), 1)
        self.assertIn("stopped, journal kept", out)
        self.assertEqual(len(embed_kb.read_journal(self.journal_path(), *self.cfg())), 1)
        self.assertFalse(self.cache_path().exists())

    def test_next_request_that_would_pass_ceiling_is_not_sent(self):
        # First batch bills just under the ceiling; the planned second would pass it.
        t = FakeTransport(usage=4_999_995)  # $0.0999999; one more small batch passes $0.10
        with mock.patch.object(embed_kb, "BATCH_MAX_INPUTS", 1):
            code, out = self.embed(t, "0.10")
        self.assertEqual(code, 4)
        self.assertEqual(len(t.bodies), 1)
        self.assertIn("next request would pass", out)


# ------------------------------------------------------------------ paid flow (fake)


class EmbedFlowTests(Base):
    def test_happy_path_bodies_cache_and_rerun(self):
        self.write_input(corpus("2", "10", "9", "7"))
        t = FakeTransport()
        with mock.patch.object(embed_kb, "BATCH_MAX_INPUTS", 3):
            code, out = self.embed(t)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.factory_calls, 1)
        self.assertEqual([len(b["input"]) for b in t.bodies], [3, 1])
        for b in t.bodies:
            self.assertEqual(set(b), {"model", "input", "dimensions", "encoding_format"})
            self.assertEqual((b["model"], b["dimensions"], b["encoding_format"]),
                             ("text-embedding-3-small", 1536, "base64"))
        preps = {r["sourceArticleId"]: embed_kb.prepare_input(
            r, FakeEncoding(), {"max_tokens": MAX, "rule": "prefix-v1"})
            for r in corpus("2", "10", "9", "7")}
        self.assertEqual(t.inputs(), [preps[s]["payload"] for s in ("2", "7", "9", "10")])
        self.assertIsInstance(preps["7"]["payload"], list)

        raw = self.cache_path().read_bytes()
        self.assertFalse(self.journal_path().exists())
        self.assertNotIn(BODY_SENTINEL.encode(), raw)
        self.assertNotIn(b"Heading", raw)
        lines = raw.split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual(json.loads(lines[0])["kind"], "header")
        self.assertEqual([json.loads(x)["source_article_id"] for x in lines[1:-1]],
                         ["2", "7", "9", "10"])
        self.assertIn(f"sha256={hashlib.sha256(raw).hexdigest()}", out)
        row7 = json.loads(lines[2])
        self.assertEqual((row7["input_kind"], row7["truncated"], row7["input_tokens"]),
                         ("tokens", True, MAX))

        code, out = self.run_main("preflight")
        self.assertIn("cache hits 4  misses 0", out)
        t2 = FakeTransport()
        code, out = self.embed(t2)
        self.assertEqual(code, 0)
        self.assertIn("nothing to embed: no request sent", out)
        self.assertEqual((t2.bodies, self.factory_calls), ([], 1))
        self.assertEqual(self.cache_path().read_bytes(), raw)

    def test_batches_respect_token_limit(self):
        self.write_input(corpus("1", "2", "3"))
        t = FakeTransport()
        sizes = [len(embed_kb.article_v1_text(r).encode()) for r in corpus("1", "2", "3")]
        limit = sizes[0] + sizes[1]  # two fit, the third does not
        with mock.patch.object(embed_kb, "BATCH_MAX_TOKENS", limit):
            code, _ = self.embed(t)
        self.assertEqual(code, 0)
        for b in t.bodies:
            self.assertLessEqual(sum(payload_tokens(p) for p in b["input"]), limit)
        self.assertEqual([len(b["input"]) for b in t.bodies], [2, 1])

    def test_crash_keeps_prior_cache_and_never_rebills(self):
        self.write_input(corpus("1", "2", "3"))
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        prior = self.cache_path().read_bytes()

        self.write_input(corpus("1", "2", "3", "4", "5"))
        t = FakeTransport(fail_on=2)
        with mock.patch.object(embed_kb, "BATCH_MAX_INPUTS", 1):
            code, out = self.embed(t)
        self.assertEqual(code, 3)
        self.assertIn("embedding request failed: OPENAI_HTTP_5XX", out)
        self.assertEqual(self.cache_path().read_bytes(), prior)
        self.assertEqual(list(embed_kb.read_journal(self.journal_path(), *self.cfg())), ["4"])

        code, out = self.run_main("preflight")
        self.assertIn("journal rows=1", out)
        self.assertIn("cache hits 4  misses 1", out)
        t2 = FakeTransport()
        self.assertEqual(self.embed(t2)[0], 0)
        self.assertEqual(len(t2.inputs()), 1)
        self.assertFalse(self.journal_path().exists())

        resumed = self.cache_path().read_bytes()
        clean = self.dir / "clean"
        self.cache_dir, saved = clean, self.cache_dir
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        self.assertEqual(self.cache_path().read_bytes(), resumed)
        self.cache_dir = saved

    def test_torn_journal_tail_is_dropped_and_rest_reused(self):
        self.write_input(corpus("1", "2", "3"))
        with mock.patch.object(embed_kb, "BATCH_MAX_INPUTS", 1):
            self.assertEqual(self.embed(FakeTransport(fail_on=3))[0], 3)
        with open(self.journal_path(), "ab") as f:
            f.write(b'{"embedding_b64":"AAAA')
        code, out = self.run_main("preflight")
        self.assertEqual(code, 0, out)
        self.assertIn("journal rows=2", out)
        t = FakeTransport()
        self.assertEqual(self.embed(t)[0], 0)
        self.assertEqual(len(t.inputs()), 1)
        cached = embed_kb.read_cache(self.cache_path(), *self.cfg())
        self.assertEqual(list(cached), ["1", "2", "3"])

    def test_torn_journal_header_restarts_journal(self):
        self.write_input(corpus("1"))
        self.cache_dir.mkdir()
        self.journal_path().write_bytes(embed_kb.header_line(*self.cfg())[:20])
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        self.assertEqual(list(embed_kb.read_cache(self.cache_path(), *self.cfg())), ["1"])

    def test_corrupt_journal_line_is_loud(self):
        self.write_input(corpus("1", "2"))
        with mock.patch.object(embed_kb, "BATCH_MAX_INPUTS", 1):
            self.embed(FakeTransport(fail_on=2))
        raw = self.journal_path().read_bytes()
        head, rest = raw.split(b"\n", 1)
        self.journal_path().write_bytes(head + b"\nnot json\n" + rest)
        t = FakeTransport()
        code, out = self.embed(t)
        self.assertEqual(code, 1)
        self.assertIn("is not valid JSON", out)
        self.assertEqual(t.bodies, [])

    def test_bad_responses_store_nothing(self):
        def drop(r):
            r["data"].pop()
            return r

        def dup_index(r):
            r["data"][0]["index"] = 1
            return r

        def set_item(key, value):
            def f(r):
                r["data"][0][key] = value
                return r
            return f

        def set_top(key, value):
            def f(r):
                r[key] = value
                return r
            return f

        mutations = {
            "wrong model": set_top("model", "text-embedding-3-large"),
            "missing item": drop,
            "duplicate index": dup_index,
            "index out of range": set_item("index", 5),
            "index as str": set_item("index", "0"),
            "short vector": set_item("embedding", base64.b64encode(b"\0" * 8).decode()),
            "float list": set_item("embedding", [0.0] * DIMS),
            "norm 2": set_item("embedding", unit_b64(0, sign=2.0)),
            "no usage": set_top("usage", {}),
            "not a dict": lambda r: [r],
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                self.cache_dir = self.dir / f"c_{uuid.uuid4().hex}"
                self.write_input(corpus("1", "2"))
                code, out = self.embed(FakeTransport(mutate=mutate))
                self.assertEqual(code, 3, out)
                self.assertIn("OPENAI_BAD_RESPONSE", out)
                self.assertFalse(self.cache_path().exists())
                self.assertEqual(embed_kb.read_journal(self.journal_path(), *self.cfg()), {})

    def test_transport_error_text_never_printed(self):
        self.write_input(corpus("1"))

        def boom(body):
            raise embed_kb.TransportError("OPENAI_HTTP_4XX")

        code, out = self.embed(boom)
        self.assertEqual(code, 3)
        self.assertIn("OPENAI_HTTP_4XX", out)


# ------------------------------------------------------------------ cache integrity


class CacheIntegrityTests(Base):
    def setUp(self):
        super().setUp()
        self.write_input(corpus("1", "2", "3"))
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        self.raw = self.cache_path().read_bytes()
        self.lines = self.raw.split(b"\n")[:-1]

    def rewrite(self, lines):
        self.cache_path().write_bytes(b"".join(x + b"\n" for x in lines))

    def assert_loud(self):
        t = FakeTransport()
        code, out = self.embed(t)
        self.assertEqual(code, 1, out)
        self.assertEqual(t.bodies, [])
        return out

    def test_rows_out_of_order(self):
        self.rewrite([self.lines[0], self.lines[2], self.lines[1], self.lines[3]])
        self.assertIn("strictly increasing", self.assert_loud())

    def test_duplicate_row(self):
        self.rewrite(self.lines + [self.lines[-1]])
        self.assert_loud()

    def test_header_for_other_config(self):
        other = json.loads(self.lines[0])
        other["config_sha256"] = "0" * 64
        self.rewrite([embed_kb.canonical_json(other)] + self.lines[1:])
        self.assertIn("header does not match", self.assert_loud())

    def test_non_canonical_bytes(self):
        row = json.loads(self.lines[1])
        self.rewrite([self.lines[0], json.dumps(row, sort_keys=True).encode()] + self.lines[2:])
        self.assertIn("not canonical", self.assert_loud())

    def test_bad_vector_row(self):
        row = json.loads(self.lines[1])
        row["embedding_b64"] = unit_b64(0, sign=2.0)
        self.rewrite([self.lines[0], embed_kb.canonical_json(row)] + self.lines[2:])
        self.assert_loud()

    def test_missing_trailing_newline(self):
        self.cache_path().write_bytes(self.raw[:-1])
        self.assert_loud()

    def test_stale_text_is_a_miss(self):
        records = corpus("1", "2", "3")
        records[0] = rec("1", full_text="Edited body.")
        self.write_input(records)
        code, out = self.run_main("preflight")
        self.assertIn("cache hits 2  misses 1 (no row 0, stale text 1, input changed 0)", out)
        t = FakeTransport()
        self.assertEqual(self.embed(t)[0], 0)
        self.assertEqual(t.inputs(), ["Title: Heading 1\n\nEdited body."])

    def test_changed_input_identity_is_a_miss(self):
        row = json.loads(self.lines[2])
        row["input_sha256"] = "f" * 64
        self.rewrite([self.lines[0], self.lines[1], embed_kb.canonical_json(row), self.lines[3]])
        code, out = self.run_main("preflight")
        self.assertIn("misses 1 (no row 0, stale text 0, input changed 1)", out)

    def test_rows_for_articles_no_longer_in_input_are_kept(self):
        self.write_input(corpus("1", "3"))
        code, out = self.run_main("preflight")
        self.assertIn("cache hits 2  misses 0", out)
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        self.assertEqual(self.cache_path().read_bytes(), self.raw)


# ------------------------------------------------------------------ real transport


class FakeResponse:
    def __init__(self, status, body=None, bad_json=False):
        self.status_code = status
        self._body = body
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError(f"cannot parse {BODY_SENTINEL}")
        return self._body


class TransportTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("socket.socket.connect", side_effect=AssertionError("network"))
        p.start()
        self.addCleanup(p.stop)

    def test_missing_key(self):
        for env in ({}, {"OPENAI_API_KEY": ""}):
            with self.assertRaises(embed_kb.EmbedError) as ctx:
                REAL_MAKE_TRANSPORT(env)
            self.assertEqual(ctx.exception.code, 2)

    def test_request_shape(self):
        send = REAL_MAKE_TRANSPORT({"OPENAI_API_KEY": FAKE_KEY})
        with mock.patch("requests.Session.post",
                        return_value=FakeResponse(200, {"ok": 1})) as post:
            self.assertEqual(send({"x": 1}), {"ok": 1})
        args, kwargs = post.call_args
        self.assertEqual(args, (embed_kb.API_URL,))
        self.assertEqual(kwargs["json"], {"x": 1})
        self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {FAKE_KEY}"})
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], embed_kb.REQUEST_TIMEOUT_S)

    def test_error_codes_carry_no_provider_text(self):
        import requests
        send = REAL_MAKE_TRANSPORT({"OPENAI_API_KEY": FAKE_KEY})
        cases = [
            (FakeResponse(429, {"error": BODY_SENTINEL}), "OPENAI_HTTP_429"),
            (FakeResponse(400, {"error": BODY_SENTINEL}), "OPENAI_HTTP_4XX"),
            (FakeResponse(401, {"error": BODY_SENTINEL}), "OPENAI_HTTP_4XX"),
            (FakeResponse(503, {"error": BODY_SENTINEL}), "OPENAI_HTTP_5XX"),
            (FakeResponse(302, None), "OPENAI_HTTP_OTHER"),
            (FakeResponse(200, bad_json=True), "OPENAI_BAD_RESPONSE"),
            (requests.Timeout(BODY_SENTINEL), "OPENAI_TIMEOUT"),
            (requests.ConnectionError(BODY_SENTINEL), "OPENAI_NETWORK"),
        ]
        for outcome, code in cases:
            with self.subTest(code=code):
                kw = ({"side_effect": outcome} if isinstance(outcome, Exception)
                      else {"return_value": outcome})
                with mock.patch("requests.Session.post", **kw):
                    with self.assertRaises(embed_kb.TransportError) as ctx:
                        send({"input": [BODY_SENTINEL]})
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(str(ctx.exception), code)
                self.assertIsNone(ctx.exception.__cause__)
                self.assertTrue(ctx.exception.__context__ is None
                                or ctx.exception.__suppress_context__)

    def test_session_ignores_environment(self):
        captured = {}
        real_session = __import__("requests").Session

        def spy():
            s = real_session()
            captured["s"] = s
            return s

        with mock.patch("requests.Session", side_effect=spy):
            REAL_MAKE_TRANSPORT({"OPENAI_API_KEY": FAKE_KEY})
        self.assertFalse(captured["s"].trust_env)


# ------------------------------------------------------------------ load (offline)


class LoadCommandTests(Base):
    def setUp(self):
        super().setUp()
        self.write_input(corpus("1", "2"))
        self.assertEqual(self.embed(FakeTransport())[0], 0)

    def test_remote_host_refused_without_connecting(self):
        self.environ["DATABASE_URL"] = "postgresql://u:p@db.example.com:5432/db"
        code, out = self.run_main("load", run_load=lambda *a: self.fail("connected"))
        self.assertEqual(code, 2)
        self.assertIn("not local", out)

    def test_journal_present_refused(self):
        self.journal_path().write_bytes(b"")
        code, out = self.run_main("load", run_load=lambda *a: self.fail("connected"))
        self.assertEqual(code, 2)
        self.assertIn("journal", out)

    def test_missing_cache_refused(self):
        self.cache_path().unlink()
        code, out = self.run_main("load", run_load=lambda *a: self.fail("connected"))
        self.assertEqual(code, 2)

    def test_load_needs_no_tokenizer_or_key(self):
        seen = {}

        def fake_run_load(url, cfg, sha, cached):
            seen.update(url=url, sha=sha, ids=list(cached))
            return {"articles": 2, "loaded": 2, "replaced": 0}

        del self.environ["OPENAI_API_KEY"]
        argv = ["--input", str(self.input), "--config", str(self.config),
                "--cache-dir", str(self.cache_dir), "load"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = embed_kb.main(argv, environ=self.environ, run_load=fake_run_load,
                                 encoding_loader=lambda *a: self.fail("tokenizer loaded"),
                                 transport_factory=lambda env: self.fail("transport"))
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(seen["ids"], ["1", "2"])
        self.assertEqual(seen["sha"], self.cfg()[1])


# ------------------------------------------------------------------ live DB


@unittest.skipUnless(URL, "KB_FINDER_TEST_DATABASE_URL not set")
class LiveDbTests(Base):
    """Throwaway schema with both migrations, one rolled-back transaction per test."""

    def setUp(self):
        super().setUp()
        self.schema = f"kbtest_{uuid.uuid4().hex}"
        self.conn = psycopg.connect(URL)
        self.addCleanup(self.rollback_and_verify)
        with psycopg.connect(URL, autocommit=True) as c:
            self.public_before = base.public_snapshot(c), base.public_article_count(c)
        cur = self.conn.cursor()
        cur.execute(sql.SQL("create schema {}").format(sql.Identifier(self.schema)))
        cur.execute(sql.SQL("set local search_path to {}").format(sql.Identifier(self.schema)))
        for name in ("articles", "article_embeddings"):
            try:
                with self.conn.transaction():
                    cur.execute(sql.SQL("select 1 from {} limit 1").format(sql.Identifier(name)))
                self.fail(f"unqualified '{name}' resolved outside the throwaway schema")
            except psycopg.errors.UndefinedTable:
                pass
        migrations = ROOT / "supabase" / "migrations"
        for name in ("20260930042716_create_articles.sql",
                     "20261001011735_add_semantic_embeddings.sql"):
            cur.execute((migrations / name).read_text(encoding="utf-8"))
        cur.close()

    def rollback_and_verify(self):
        self.conn.rollback()
        self.conn.close()
        with psycopg.connect(URL, autocommit=True) as c:
            exists = c.execute("select exists (select 1 from pg_namespace where nspname = %s)",
                               [self.schema]).fetchone()[0]
            self.assertFalse(exists)
            self.assertEqual((base.public_snapshot(c), base.public_article_count(c)),
                             self.public_before)

    def test_sql_hash_equals_python_on_every_real_article(self):
        raw = embed_kb.DEFAULT_INPUT.read_text(encoding="utf-8").splitlines()
        records, errors = ingest_kb.load_records(raw)
        self.assertEqual(errors, [])
        ingest_kb.sync(self.conn, [ingest_kb.to_row(r) for r in records], False)
        with self.conn.cursor() as cur:
            cur.execute("select source_article_id, kb_article_v1_sha256(title, full_text) "
                        "from articles")
            sql_hashes = dict(cur.fetchall())
        py = {r["sourceArticleId"]: hashlib.sha256(
            embed_kb.article_v1_text(r).encode("utf-8")).hexdigest() for r in records}
        non_ascii = [r["sourceArticleId"] for r in records
                     if not embed_kb.article_v1_text(r).isascii()]
        longest = max(records, key=lambda r: len(embed_kb.article_v1_text(r).encode()))
        self.assertGreater(len(non_ascii), 0)
        self.assertGreater(len(embed_kb.article_v1_text(longest).encode()), 70_000)
        self.assertEqual(len(sql_hashes), len(records))
        mismatched = sorted(s for s in py if sql_hashes.get(s) != py[s])
        self.assertEqual(mismatched, [])
        print(f"\n  real-article hash check: {len(records)} articles, {len(non_ascii)} "
              f"non-ASCII, longest {longest['sourceArticleId']} "
              f"({len(embed_kb.article_v1_text(longest).encode())} bytes): all equal",
              file=sys.stderr)

    def test_load_rows_then_semantic_search(self):
        records = corpus("1", "2", "7")
        self.write_input(records)
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        ingest_kb.sync(self.conn, base.rows(*records), False)
        cfg, sha = self.cfg()
        cached = embed_kb.read_cache(self.cache_path(), cfg, sha)
        c = embed_kb.load_rows(self.conn, cfg, sha, cached)
        self.assertEqual(c, {"articles": 3, "loaded": 3, "replaced": 0})
        with self.conn.cursor() as cur:
            cur.execute("select input_kind, truncated, input_tokens from article_embeddings "
                        "where source_article_id = '7'")
            self.assertEqual(cur.fetchone(), ("tokens", True, MAX))
            cur.execute("select embedding::text from article_embeddings "
                        "where source_article_id = '2'")
            stored = [float(x) for x in cur.fetchone()[0][1:-1].split(",")]
        self.assertEqual(stored, list(embed_kb.decode_embedding(cached["2"]["embedding_b64"],
                                                                DIMS)))
        probe = embed_kb.vector_literal(embed_kb.decode_embedding(cached["2"]["embedding_b64"],
                                                                  DIMS))
        with self.conn.cursor() as cur:
            cur.execute("select source_article_id, score from kb_search_semantic("
                        "%s::text::extensions.vector, 50, %s)", [probe, sha])
            rows = cur.fetchall()
        self.assertEqual(rows[0], ("2", 1.0))
        self.assertEqual(len(rows), 3)
        self.assertEqual(embed_kb.load_rows(self.conn, cfg, sha, cached)["replaced"], 3)

    def test_load_refuses_incomplete_cache_and_writes_nothing(self):
        records = corpus("1", "2")
        self.write_input(records[:1])
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        ingest_kb.sync(self.conn, base.rows(*records), False)
        cfg, sha = self.cfg()
        cached = embed_kb.read_cache(self.cache_path(), cfg, sha)
        with self.assertRaises(embed_kb.EmbedError) as ctx:
            with self.conn.transaction():
                embed_kb.load_rows(self.conn, cfg, sha, cached)
        self.assertIn("1 articles have no valid cached embedding (first: 2)", str(ctx.exception))
        with self.conn.cursor() as cur:
            cur.execute("select count(*) from article_embeddings")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_load_refuses_stale_cache_row(self):
        records = corpus("1")
        self.write_input(records)
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        ingest_kb.sync(self.conn, base.rows(rec("1", full_text="Changed in the DB.")), False)
        cfg, sha = self.cfg()
        with self.assertRaises(embed_kb.EmbedError):
            embed_kb.load_rows(self.conn, cfg, sha,
                               embed_kb.read_cache(self.cache_path(), cfg, sha))

    def test_load_refuses_conflicting_config_row(self):
        records = corpus("1")
        self.write_input(records)
        self.assertEqual(self.embed(FakeTransport())[0], 0)
        ingest_kb.sync(self.conn, base.rows(*records), False)
        cfg, sha = self.cfg()
        with self.conn.cursor() as cur:
            cur.execute("insert into embedding_configs values (%s, '{\"other\": 1}', "
                        "'text-embedding-3-small', 1536, 'article-v1')", [sha])
        with self.assertRaises(embed_kb.EmbedError) as ctx:
            embed_kb.load_rows(self.conn, cfg, sha,
                               embed_kb.read_cache(self.cache_path(), cfg, sha))
        self.assertIn("different config", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
