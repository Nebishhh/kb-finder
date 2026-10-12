#!/usr/bin/env python
"""Phase 5: embed every article once (article-v1), cache the vectors, load them into Postgres.

Input  : data/parsed/articles.jsonl, validated with ingest_kb.load_records
Config : config/embedding/article-v1.json (canonical bytes; config_sha256 = sha256 of them),
         or --config config/embedding/article-v2.json (article-v2 experiment, DECISIONS.md
         2026-10-07). The file's template_version picks the text builder and SQL functions.
Cache  : cache/article_embeddings/<first 16 hex of config_sha256>.jsonl (gitignored), so
         each config has its own cache file
Encoder: cl100k_base, file kept in cache/tiktoken/ and checked against the config pin

Run:  .venv/Scripts/python scripts/embed_kb.py [--config PATH] preflight
      .venv/Scripts/python scripts/embed_kb.py [--config PATH] embed --paid --max-cost-usd X
                                                                        (human only)
      .venv/Scripts/python scripts/embed_kb.py [--config PATH] load [--allow-remote]

Subcommands
-----------
preflight  Default. No API call, no database. Prints article and token counts, cache
           hits and misses, tokens to embed and projected cost. Never reads
           OPENAI_API_KEY. May download the public cl100k_base file once (no key).
embed      Paid. Refuses without --paid. --max-cost-usd is required, may not exceed
           HARD_CAP_USD, and the run aborts before the first request if the projected
           cost exceeds it or the token count exceeds HARD_CAP_TOKENS. After every
           response the cumulative tokens the API reports billed are checked against
           both HARD_CAP_TOKENS and --max-cost-usd; passing either stops the run.
           Planned vs billed tokens are printed per request and in the summary; a
           mismatch is reported, not a reason to stop. Writes the cache only; never
           connects to the database. No automatic retries.
load       No API call. DATABASE_URL only; non-local hosts are refused unless
           --allow-remote. One transaction: config row, this config's embeddings
           replaced, then kb_search_semantic (article-v2: kb_search_semantic_v2) is
           called once so every KBF check runs. Any failure rolls back.

article-v1 is exactly "Title: {title}\\n\\n{fullText}". No tags, description, category,
URL, staffOnly or section fields. (Lexical indexes title, tags, description, fullText.)

article-v2 is the article-v1 text, then "\\n\\nStaff only:\\n" + staffOnly.text, untrimmed,
only when that text is a string with content after stripping exactly the six characters
in STAFF_TRIM. No suffix when staffOnly is null, has no "text", or "text" is null. Any
other "text" type is an error, never skipped. The bytes match kb_article_v2_sha256
(supabase/migrations/*_add_article_v2.sql).

Input rule (text-if-fits-else-token-ids-v1): the text is encoded with cl100k_base,
special-token text encoded as ordinary text. If it fits in max_tokens it is sent as
the string; otherwise the first max_tokens token IDs are sent. input_sha256 is
sha256(b"text\\n" + UTF-8 text) or sha256(b"tokens\\n" + compact JSON of the IDs):
exactly what is sent.

Cache  Line 1 is a header holding the config. Then one row per article, sorted by
       numeric sourceArticleId, canonical JSON (sorted keys, no spaces, ASCII), LF.
       No article text and no timestamps. The prior cache is never opened for
       writing: each finished batch is appended and fsynced to <cache>.journal; at the
       end prior rows + journal rows are written to <cache>.tmp, fsynced and moved
       over the cache with os.replace, then the journal is removed. A rerun after a
       crash reuses journal rows, so finished batches are never billed twice.

Output Counts, hashes and fixed error codes only. Article text, request bodies,
       provider error text and the API key are never printed.
Exit   0 ok, 1 invalid input or cache, 2 usage/config, 3 API or database error,
       4 cost guard stopped the run.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import struct
import sys
from pathlib import Path

import ingest_kb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "parsed" / "articles.jsonl"
CONFIG_PATH = ROOT / "config" / "embedding" / "article-v1.json"
CACHE_DIR = ROOT / "cache" / "article_embeddings"
TIKTOKEN_DIR = ROOT / "cache" / "tiktoken"

API_URL = "https://api.openai.com/v1/embeddings"
ENCODING_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
REQUEST_TIMEOUT_S = 60

# https://developers.openai.com/api/docs/models/text-embedding-3-small ($0.02 / 1M tokens).
PRICE_USD_PER_1M_TOKENS = 0.02
HARD_CAP_USD = 0.50
HARD_CAP_TOKENS = 2_000_000
# Small batches: a crash loses at most one batch of spend. API limits are 2048 inputs
# and 300,000 tokens per request.
BATCH_MAX_INPUTS = 100
BATCH_MAX_TOKENS = 100_000
API_MAX_INPUT_TOKENS = 8192

TEMPLATE = "Title: {title}\n\n{fullText}"
TEMPLATE_V2 = TEMPLATE + "\n\nStaff only:\n{staffOnly.text}"
# The characters the article-v2 suffix rule strips (btrim set in kb_article_v2_sha256).
STAFF_TRIM = " \t\n\x0b\x0c\r"
NORM_TOLERANCE = 1e-3

_SHARED_CONFIG = {
    "dimensions": 1536,
    "encoding": "cl100k_base",
    "input_hash": "sha256-kind-prefixed-v1",
    "input_serialization": "text-if-fits-else-token-ids-v1",
    "model": "text-embedding-3-small",
    "newline": "LF",
    "query_input": "raw-query-string-v1",
    "response_encoding": "base64-float32le",
    "special_tokens": "encode_as_text",
}

# Values the code implements, by template_version. The config must name exactly one.
ALLOWED_CONFIGS = {
    "article-v1": {**_SHARED_CONFIG, "staff_only": "excluded", "template": TEMPLATE,
                   "template_version": "article-v1"},
    "article-v2": {**_SHARED_CONFIG, "staff_only": "suffix-if-text-nonblank-v1",
                   "template": TEMPLATE_V2, "template_version": "article-v2"},
}

# What load runs, by template_version: the SQL text hash per article, then one probe
# search so every KBF check runs. Fixed strings; nothing is formatted in.
LOAD_SQL = {
    "article-v1": ("select source_article_id, kb_article_v1_sha256(title, full_text) "
                   "from articles",
                   "select count(*) from kb_search_semantic(%s::text::extensions.vector, "
                   "1, %s)"),
    "article-v2": ("select source_article_id, kb_article_v2_sha256(title, full_text, "
                   "staff_only) from articles",
                   "select count(*) from kb_search_semantic_v2(%s::text::extensions.vector, "
                   "1, %s)"),
}
TRUNCATION_RULES = ("prefix-v1", "none-abort")
HEX64 = frozenset("0123456789abcdef")
ROW_KEYS = {"embedding_b64", "input_kind", "input_sha256", "input_tokens", "kind",
            "source_article_id", "source_text_sha256", "truncated"}

EXIT_OK, EXIT_INVALID, EXIT_USAGE, EXIT_EXTERNAL, EXIT_COST = 0, 1, 2, 3, 4


class EmbedError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class TransportError(Exception):
    """Fixed code only. Never carries provider text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ config


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _is_hex64(v) -> bool:
    return isinstance(v, str) and len(v) == 64 and set(v) <= HEX64


def load_config(path: Path) -> tuple[dict, str]:
    """Return (config, config_sha256). Fails unless the file is canonical and names
    exactly what this code implements."""
    try:
        raw = path.read_bytes()
        cfg = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise EmbedError(f"config: cannot read {path} as ASCII JSON", EXIT_USAGE) from None
    if not isinstance(cfg, dict) or canonical_json(cfg) != raw:
        raise EmbedError("config: file is not canonical JSON", EXIT_USAGE)
    version = cfg.get("template_version")
    fixed = ALLOWED_CONFIGS.get(version) if isinstance(version, str) else None
    if fixed is None:
        raise EmbedError("config: template_version is not one this code implements",
                         EXIT_USAGE)
    expected = set(fixed) | {"encoding_file_sha256", "truncation"}
    if set(cfg) != expected:
        raise EmbedError("config: unexpected or missing keys", EXIT_USAGE)
    for k, v in fixed.items():
        if cfg[k] != v or type(cfg[k]) is not type(v):
            raise EmbedError(f"config: {k} is not the value this code implements", EXIT_USAGE)
    if not _is_hex64(cfg["encoding_file_sha256"]):
        raise EmbedError("config: encoding_file_sha256 must be 64 lowercase hex", EXIT_USAGE)
    t = cfg["truncation"]
    if (not isinstance(t, dict) or set(t) != {"max_tokens", "rule"}
            or t["rule"] not in TRUNCATION_RULES
            or type(t["max_tokens"]) is not int
            or not 1 <= t["max_tokens"] <= API_MAX_INPUT_TOKENS):
        raise EmbedError("config: truncation must be {max_tokens: 1..8192, rule: "
                         "prefix-v1|none-abort}", EXIT_USAGE)
    return cfg, hashlib.sha256(raw).hexdigest()


# ------------------------------------------------------------------ encoding


def encoding_cache_path(cache_dir: Path) -> Path:
    # tiktoken names cached files by sha1 of the source URL.
    return cache_dir / hashlib.sha1(ENCODING_URL.encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_encoding(cache_dir: Path, pinned_sha256: str, allow_download: bool = True):
    """cl100k_base from cache_dir, checked against the pin before tiktoken reads it.
    (tiktoken would silently re-download a cached file that fails its own check.)"""
    path = encoding_cache_path(cache_dir)
    if path.exists():
        if _file_sha256(path) != pinned_sha256:
            raise EmbedError(f"encoding file sha256 mismatch: {path}", EXIT_INVALID)
    elif not allow_download:
        raise EmbedError(f"encoding file missing: {path}", EXIT_USAGE)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)  # set here, not by the shell
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    if not path.exists() or _file_sha256(path) != pinned_sha256:
        raise EmbedError(f"encoding file sha256 mismatch after load: {path}", EXIT_INVALID)
    return enc


def encode_tokens(enc, text: str) -> list[int]:
    # Special-token text such as "<|endoftext|>" is encoded as ordinary text.
    return enc.encode(text, allowed_special=set(), disallowed_special=())


# ------------------------------------------------------------------ inputs


def article_v1_text(rec: dict) -> str:
    return f"Title: {rec['title']}\n\n{rec['fullText']}"


def article_v2_text(rec: dict) -> str:
    """article-v1 text plus the staff-only suffix, byte for byte what
    kb_article_v2_sha256 hashes. Errors name the article, never its text."""
    text = article_v1_text(rec)
    sid = rec["sourceArticleId"]
    if "staffOnly" not in rec:
        raise EmbedError(f"article {sid}: staffOnly is missing", EXIT_INVALID)
    staff = rec["staffOnly"]
    if staff is None:
        return text
    if not isinstance(staff, dict):
        raise EmbedError(f"article {sid}: staffOnly is not null or an object", EXIT_INVALID)
    staff_text = staff.get("text")
    if staff_text is None:
        return text
    if not isinstance(staff_text, str):
        raise EmbedError(f"article {sid}: staffOnly.text is not a string or null",
                         EXIT_INVALID)
    if staff_text.strip(STAFF_TRIM) == "":
        return text
    return text + "\n\nStaff only:\n" + staff_text


TEXT_BUILDERS = {"article-v1": article_v1_text, "article-v2": article_v2_text}


def text_builder(cfg: dict):
    return TEXT_BUILDERS[cfg["template_version"]]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def input_sha256(kind: str, payload) -> str:
    if kind == "text":
        return sha256_hex(b"text\n" + payload.encode("utf-8"))
    return sha256_hex(b"tokens\n" + json.dumps(payload, separators=(",", ":")).encode("ascii"))


def prepare_input(rec: dict, enc, truncation: dict, build_text=article_v1_text) -> dict:
    """What is sent for one article, plus the identity of exactly that. build_plan
    passes the config's builder (text_builder)."""
    text = build_text(rec)
    tokens = encode_tokens(enc, text)
    max_tokens = truncation["max_tokens"]
    if len(tokens) <= max_tokens:
        kind, payload, n = "text", text, len(tokens)
    elif truncation["rule"] == "prefix-v1":
        kind, payload, n = "tokens", tokens[:max_tokens], max_tokens
    else:
        raise EmbedError(f"article {rec['sourceArticleId']} exceeds {max_tokens} tokens "
                         "and the truncation rule is none-abort", EXIT_INVALID)
    return {
        "source_article_id": rec["sourceArticleId"],
        "source_text_sha256": sha256_hex(text.encode("utf-8")),
        "full_tokens": len(tokens),
        "input_kind": kind,
        "input_tokens": n,
        "truncated": kind == "tokens",
        "input_sha256": input_sha256(kind, payload),
        "payload": payload,
    }


# ------------------------------------------------------------------ vectors


def decode_embedding(b64, dims: int) -> tuple[float, ...]:
    """base64 of dims little-endian float32 (the official SDK's default decoding).
    Requires exact length, all finite, unit norm within NORM_TOLERANCE."""
    if not isinstance(b64, str):
        raise ValueError("embedding is not a base64 string")
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        raise ValueError("embedding is not valid base64") from None
    if len(raw) != dims * 4:
        raise ValueError("embedding has the wrong byte length")
    values = struct.unpack(f"<{dims}f", raw)
    if not all(math.isfinite(x) for x in values):
        raise ValueError("embedding has a non-finite value")
    norm = math.sqrt(math.fsum(x * x for x in values))
    if abs(norm - 1.0) > NORM_TOLERANCE:
        raise ValueError("embedding norm is not about 1")
    return values


def canonical_b64(values: tuple[float, ...]) -> str:
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def vector_literal(values) -> str:
    # repr of a float32 value is exact; pgvector parses it back to the same float32.
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


# ------------------------------------------------------------------ cache


def cache_path_for(cache_dir: Path, config_sha256: str) -> Path:
    return cache_dir / f"{config_sha256[:16]}.jsonl"


def header_line(cfg: dict, config_sha256: str) -> bytes:
    return canonical_json({"config": cfg, "config_sha256": config_sha256,
                           "kind": "header"}) + b"\n"


def make_row(prep: dict, values: tuple[float, ...]) -> dict:
    return {
        "embedding_b64": canonical_b64(values),
        "input_kind": prep["input_kind"],
        "input_sha256": prep["input_sha256"],
        "input_tokens": prep["input_tokens"],
        "kind": "row",
        "source_article_id": prep["source_article_id"],
        "source_text_sha256": prep["source_text_sha256"],
        "truncated": prep["truncated"],
    }


def check_row(row, dims: int) -> str | None:
    """Rule violated by one cache row, or None. Messages never include values."""
    if not isinstance(row, dict) or set(row) != ROW_KEYS or row["kind"] != "row":
        return "row keys"
    sid = row["source_article_id"]
    if not (isinstance(sid, str) and ingest_kb.ID_RE.match(sid)):
        return "source_article_id"
    if not (_is_hex64(row["source_text_sha256"]) and _is_hex64(row["input_sha256"])):
        return "hash fields"
    if row["input_kind"] not in ("text", "tokens"):
        return "input_kind"
    if type(row["truncated"]) is not bool or row["truncated"] != (row["input_kind"] == "tokens"):
        return "truncated"
    if type(row["input_tokens"]) is not int or not 1 <= row["input_tokens"] <= API_MAX_INPUT_TOKENS:
        return "input_tokens"
    try:
        values = decode_embedding(row["embedding_b64"], dims)
    except ValueError as exc:
        return str(exc)
    if canonical_b64(values) != row["embedding_b64"]:
        return "embedding_b64 not canonical"
    return None


def _parse_lines(raw: bytes, label: str, allow_torn_tail: bool) -> list:
    if not raw:
        return []
    lines = raw.split(b"\n")
    torn = lines.pop()  # b"" when the file ends with a newline
    out = []
    for n, line in enumerate(lines, 1):
        try:
            out.append(json.loads(line.decode("ascii")))
        except (UnicodeDecodeError, ValueError):
            raise EmbedError(f"{label}: line {n} is not valid JSON", EXIT_INVALID) from None
    if torn and not allow_torn_tail:
        raise EmbedError(f"{label}: last line has no newline", EXIT_INVALID)
    return out


def read_cache(path: Path, cfg: dict, config_sha256: str) -> dict[str, dict]:
    """Rows by source_article_id. Missing file: {}. Any defect: loud error, so a damaged
    cache can never turn into a silent re-embed."""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if not raw.startswith(header_line(cfg, config_sha256)):
        raise EmbedError(f"cache {path}: header does not match the active config", EXIT_INVALID)
    rows = _parse_lines(raw, f"cache {path}", allow_torn_tail=False)[1:]
    out, prev = {}, 0
    for n, row in enumerate(rows, 2):
        problem = check_row(row, cfg["dimensions"])
        if problem:
            raise EmbedError(f"cache {path}: line {n}: {problem}", EXIT_INVALID)
        sid = int(row["source_article_id"])
        if sid <= prev:
            raise EmbedError(f"cache {path}: line {n}: rows not in strictly increasing "
                             "sourceArticleId order", EXIT_INVALID)
        prev = sid
        out[row["source_article_id"]] = row
    if serialize_cache(cfg, config_sha256, out) != raw:
        raise EmbedError(f"cache {path}: bytes are not canonical", EXIT_INVALID)
    return out


def read_journal(path: Path, cfg: dict, config_sha256: str) -> dict[str, dict]:
    """Rows from an interrupted run. A torn last line (crash mid-write) is dropped;
    any other defect is a loud error."""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    head = header_line(cfg, config_sha256)
    if not raw.startswith(head):
        if head.startswith(raw):  # crashed while writing the header
            return {}
        raise EmbedError(f"journal {path}: header does not match the active config",
                         EXIT_INVALID)
    out = {}
    for n, row in enumerate(_parse_lines(raw, f"journal {path}", allow_torn_tail=True)[1:], 2):
        problem = check_row(row, cfg["dimensions"])
        if problem:
            raise EmbedError(f"journal {path}: line {n}: {problem}", EXIT_INVALID)
        out[row["source_article_id"]] = row  # a later row for the same ID wins
    return out


def serialize_cache(cfg: dict, config_sha256: str, rows: dict[str, dict]) -> bytes:
    body = b"".join(canonical_json(rows[sid]) + b"\n"
                    for sid in sorted(rows, key=int))
    return header_line(cfg, config_sha256) + body


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Journal:
    def __init__(self, path: Path, cfg: dict, config_sha256: str):
        self.path = path
        if path.exists():
            # Drop a torn last line (or torn header) left by a crash, so the next
            # append starts on a fresh line. Complete lines were already validated.
            raw = path.read_bytes()
            keep = raw.rfind(b"\n") + 1
            if keep != len(raw):
                with open(path, "r+b") as f:
                    f.truncate(keep)
                    f.flush()
                    os.fsync(f.fileno())
        new = not path.exists() or path.stat().st_size == 0
        self.f = open(path, "ab")
        if new:
            self._append(header_line(cfg, config_sha256))

    def _append(self, data: bytes) -> None:
        self.f.write(data)
        self.f.flush()
        os.fsync(self.f.fileno())

    def add(self, rows: list[dict]) -> None:
        self._append(b"".join(canonical_json(r) + b"\n" for r in rows))

    def close(self) -> None:
        self.f.close()


def classify(preps: list[dict], cached: dict[str, dict]) -> dict:
    """Split articles into hits and misses (with the reason for each miss)."""
    hits, misses = [], []
    reasons = {"no_row": 0, "stale_text": 0, "input_changed": 0}
    for p in preps:
        row = cached.get(p["source_article_id"])
        if row is None:
            reasons["no_row"] += 1
        elif row["source_text_sha256"] != p["source_text_sha256"]:
            reasons["stale_text"] += 1
        elif (row["input_sha256"], row["input_kind"], row["input_tokens"]) != (
                p["input_sha256"], p["input_kind"], p["input_tokens"]):
            reasons["input_changed"] += 1
        else:
            hits.append(p)
            continue
        misses.append(p)
    return {"hits": hits, "misses": misses, "reasons": reasons}


# ------------------------------------------------------------------ plan


def nearest_rank(sorted_values: list[int], q: float) -> int:
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def cost_usd(tokens: int) -> float:
    return tokens * PRICE_USD_PER_1M_TOKENS / 1_000_000


INPUT_KINDS = ("text", "tokens")


def batches(preps: list[dict]) -> list[list[dict]]:
    """Requests in send order. A request holds one input kind only: the API takes an
    array of strings or an array of token arrays, never a mixture. String inputs come
    first, then token inputs, each in article order, each under the same limits.
    Batching never changes an article's input, only which request carries it."""
    out = []
    for kind in INPUT_KINDS:
        cur, cur_tokens = [], 0
        for p in preps:
            if p["input_kind"] != kind:
                continue
            if cur and (len(cur) >= BATCH_MAX_INPUTS
                        or cur_tokens + p["input_tokens"] > BATCH_MAX_TOKENS):
                out.append(cur)
                cur, cur_tokens = [], 0
            cur.append(p)
            cur_tokens += p["input_tokens"]
        if cur:
            out.append(cur)
    if sum(len(b) for b in out) != len(preps):
        raise EmbedError("internal: an input has an unknown kind", EXIT_INVALID)
    return out


def build_plan(args, cfg, config_sha256, enc) -> dict:
    if not args.input.is_file():
        raise EmbedError(f"input not found: {args.input}", EXIT_USAGE)
    raw = args.input.read_bytes()
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise EmbedError("input is not UTF-8", EXIT_INVALID) from None
    records, errors = ingest_kb.load_records(lines)
    if errors:
        for e in errors:
            print(e)
        raise EmbedError(f"input validation failed: {len(errors)} errors", EXIT_INVALID)
    build = text_builder(cfg)
    preps = [prepare_input(r, enc, cfg["truncation"], build) for r in records]
    cache_path = cache_path_for(args.cache_dir, config_sha256)
    journal_path = cache_path.with_name(cache_path.name + ".journal")
    cached = read_cache(cache_path, cfg, config_sha256)
    journaled = read_journal(journal_path, cfg, config_sha256)
    known = dict(cached)
    known.update(journaled)
    split = classify(preps, known)
    return {"input_sha256": sha256_hex(raw), "preps": preps, "cache_path": cache_path,
            "journal_path": journal_path, "cached": cached, "journaled": journaled,
            **split}


def print_plan(plan: dict, cfg: dict, config_sha256: str, args) -> None:
    preps = plan["preps"]
    full = sorted(p["full_tokens"] for p in preps)
    max_tokens = cfg["truncation"]["max_tokens"]
    to_embed = sum(p["input_tokens"] for p in plan["misses"])
    r = plan["reasons"]
    print(f"input {args.input} sha256={plan['input_sha256']} articles={len(preps)}")
    print(f"config {args.config} sha256={config_sha256}")
    print(f"model {cfg['model']} dimensions={cfg['dimensions']} "
          f"template {cfg['template_version']}")
    print(f"encoding {cfg['encoding']} file sha256={cfg['encoding_file_sha256']} (verified)")
    print(f"{cfg['template_version']} tokens before truncation: max {full[-1]} "
          f"p50 {nearest_rank(full, 0.5)} p95 {nearest_rank(full, 0.95)} total {sum(full)} "
          f"over_{max_tokens} {sum(t > max_tokens for t in full)}")
    print(f"truncation {cfg['truncation']['rule']} max_tokens={max_tokens}: "
          f"truncated {sum(p['truncated'] for p in preps)}, "
          f"tokens sent for all articles {sum(p['input_tokens'] for p in preps)}")
    state = "present" if plan["cache_path"].exists() else "absent"
    print(f"cache {plan['cache_path']} {state} rows={len(plan['cached'])}; "
          f"journal rows={len(plan['journaled'])}")
    print(f"cache hits {len(plan['hits'])}  misses {len(plan['misses'])} "
          f"(no row {r['no_row']}, stale text {r['stale_text']}, "
          f"input changed {r['input_changed']})")
    print(f"tokens to embed {to_embed} in {len(batches(plan['misses']))} requests")
    print(f"projected cost ${cost_usd(to_embed):.6f} at ${PRICE_USD_PER_1M_TOKENS} per 1M tokens "
          f"(hard caps ${HARD_CAP_USD:.2f}, {HARD_CAP_TOKENS} tokens)")


# ------------------------------------------------------------------ transport


def make_transport(environ):
    """The only place OPENAI_API_KEY is read. Returns send(body) -> parsed JSON."""
    key = environ.get("OPENAI_API_KEY")
    if not key:
        raise EmbedError("OPENAI_API_KEY is not set in this shell", EXIT_USAGE)
    import requests
    session = requests.Session()
    session.trust_env = False  # no .netrc or proxy variables can change auth or route

    def send(body: dict) -> object:
        try:
            resp = session.post(API_URL, json=body, timeout=REQUEST_TIMEOUT_S,
                                allow_redirects=False,
                                headers={"Authorization": f"Bearer {key}"})
        except requests.Timeout:
            raise TransportError("OPENAI_TIMEOUT") from None
        except requests.RequestException:
            raise TransportError("OPENAI_NETWORK") from None
        status = resp.status_code
        if status == 429:
            raise TransportError("OPENAI_HTTP_429")
        if 400 <= status < 500:
            raise TransportError("OPENAI_HTTP_4XX")
        if status >= 500:
            raise TransportError("OPENAI_HTTP_5XX")
        if status != 200:
            raise TransportError("OPENAI_HTTP_OTHER")
        try:
            return resp.json()
        except ValueError:
            raise TransportError("OPENAI_BAD_RESPONSE") from None

    return send


def parse_response(resp, batch: list[dict], cfg: dict) -> tuple[list[tuple], int]:
    """Vectors in batch order and the billed token count, or TransportError."""
    try:
        if not isinstance(resp, dict) or resp.get("model") != cfg["model"]:
            raise ValueError
        data = resp["data"]
        if not isinstance(data, list) or len(data) != len(batch):
            raise ValueError
        by_index = {}
        for item in data:
            i = item["index"]
            if type(i) is not int or not 0 <= i < len(batch) or i in by_index:
                raise ValueError
            by_index[i] = decode_embedding(item["embedding"], cfg["dimensions"])
        used = resp["usage"]["total_tokens"]
        if type(used) is not int or used < 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise TransportError("OPENAI_BAD_RESPONSE") from None
    return [by_index[i] for i in range(len(batch))], used


# ------------------------------------------------------------------ commands


def cmd_preflight(args, cfg, config_sha256, enc) -> int:
    plan = build_plan(args, cfg, config_sha256, enc)
    print_plan(plan, cfg, config_sha256, args)
    print("preflight only: no API call made")
    return EXIT_OK


def _mismatch(planned: int, billed: int) -> str:
    return "" if planned == billed else f" (billed {billed - planned:+d} vs planned)"


def cmd_embed(args, cfg, config_sha256, enc, environ, transport_factory) -> int:
    if not args.paid:
        raise EmbedError("embed makes paid API calls; pass --paid to confirm", EXIT_USAGE)
    ceiling = args.max_cost_usd
    if not (math.isfinite(ceiling) and 0 < ceiling <= HARD_CAP_USD):
        raise EmbedError(f"--max-cost-usd must be > 0 and <= {HARD_CAP_USD:.2f}", EXIT_USAGE)
    plan = build_plan(args, cfg, config_sha256, enc)
    print_plan(plan, cfg, config_sha256, args)
    misses = plan["misses"]
    to_embed = sum(p["input_tokens"] for p in misses)
    if to_embed > HARD_CAP_TOKENS:
        raise EmbedError(f"projected tokens {to_embed} exceed the hard cap {HARD_CAP_TOKENS}; "
                         "no request sent", EXIT_COST)
    if cost_usd(to_embed) > ceiling:
        raise EmbedError(f"projected cost ${cost_usd(to_embed):.6f} exceeds --max-cost-usd "
                         f"{ceiling}; no request sent", EXIT_COST)

    rows = dict(plan["cached"])
    rows.update(plan["journaled"])
    if misses:
        send = transport_factory(environ)
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        journal = Journal(plan["journal_path"], cfg, config_sha256)
        billed = planned_total = requests_sent = 0
        try:
            for batch in batches(misses):
                planned = sum(p["input_tokens"] for p in batch)
                if cost_usd(billed + planned) > ceiling:
                    raise EmbedError(f"next request would pass --max-cost-usd {ceiling}; "
                                     "stopped", EXIT_COST)
                body = {"model": cfg["model"], "input": [p["payload"] for p in batch],
                        "dimensions": cfg["dimensions"], "encoding_format": "base64"}
                try:
                    resp = send(body)
                    requests_sent += 1
                    vectors, used = parse_response(resp, batch, cfg)
                except TransportError as exc:
                    raise EmbedError(f"embedding request failed: {exc.code}; "
                                     f"{requests_sent} requests answered by the API, journal kept",
                                     EXIT_EXTERNAL) from None
                new_rows = [make_row(p, v) for p, v in zip(batch, vectors)]
                journal.add(new_rows)
                rows.update((r["source_article_id"], r) for r in new_rows)
                billed += used
                planned_total += planned
                # A planned/billed mismatch is reported, never a reason to stop; the
                # caps below are checked against what the API says it billed.
                print(f"request {requests_sent}: {len(batch)} articles, "
                      f"tokens planned {planned} billed {used}"
                      f"{_mismatch(planned, used)}")
                if billed > HARD_CAP_TOKENS:
                    raise EmbedError(f"billed tokens {billed} (planned {planned_total}) passed "
                                     f"the hard cap {HARD_CAP_TOKENS}; stopped, journal kept",
                                     EXIT_COST)
                if cost_usd(billed) > ceiling:
                    raise EmbedError(f"billed tokens {billed} (planned {planned_total}) passed "
                                     f"--max-cost-usd {ceiling}; stopped, journal kept",
                                     EXIT_COST)
        finally:
            journal.close()
        print(f"requests {requests_sent}, tokens planned {planned_total} billed {billed}"
              f"{_mismatch(planned_total, billed)}, cost ${cost_usd(billed):.6f}")
    else:
        print("nothing to embed: no request sent")

    data = serialize_cache(cfg, config_sha256, rows)
    if misses or plan["journaled"] or not plan["cache_path"].exists():
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        write_atomic(plan["cache_path"], data)
        if plan["journal_path"].exists():
            plan["journal_path"].unlink()
    print(f"cache {plan['cache_path']} rows={len(rows)} sha256={sha256_hex(data)}")
    return EXIT_OK


def load_rows(conn, cfg: dict, config_sha256: str, cached: dict[str, dict]) -> dict:
    """Replace this config's embeddings with the cached rows for every DB article, then
    run kb_search_semantic (article-v2: kb_search_semantic_v2) once. Caller owns the
    transaction."""
    hash_sql, probe_sql = LOAD_SQL[cfg["template_version"]]
    with conn.cursor() as cur:
        cur.execute("select config from embedding_configs where config_sha256 = %s",
                    [config_sha256])
        got = cur.fetchone()
        if got is None:
            from psycopg.types.json import Jsonb
            cur.execute("insert into embedding_configs (config_sha256, config, model, dimensions, "
                        "template_version) values (%s, %s, %s, %s, %s)",
                        [config_sha256, Jsonb(cfg), cfg["model"], cfg["dimensions"],
                         cfg["template_version"]])
        elif got[0] != cfg:
            raise EmbedError("embedding_configs row for this sha256 holds a different config",
                             EXIT_INVALID)
        cur.execute(hash_sql)
        db = dict(cur.fetchall())
        missing = sorted((sid for sid, h in db.items()
                          if sid not in cached or cached[sid]["source_text_sha256"] != h),
                         key=int)
        if missing:
            raise EmbedError(f"{len(missing)} articles have no valid cached embedding "
                             f"(first: {missing[0]}); run embed first", EXIT_INVALID)
        cur.execute("delete from article_embeddings where config_sha256 = %s", [config_sha256])
        replaced = cur.rowcount
        dims = cfg["dimensions"]
        cur.executemany(
            "insert into article_embeddings (config_sha256, source_article_id, "
            "source_text_sha256, input_kind, input_sha256, input_tokens, truncated, embedding) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s::text::extensions.vector)",
            [[config_sha256, sid, r["source_text_sha256"], r["input_kind"], r["input_sha256"],
              r["input_tokens"], r["truncated"],
              vector_literal(decode_embedding(r["embedding_b64"], dims))]
             for sid, r in sorted(((s, cached[s]) for s in db), key=lambda x: int(x[0]))])
        probe = vector_literal(decode_embedding(cached[min(db, key=int)]["embedding_b64"], dims)) \
            if db else None
        if probe is not None:
            cur.execute(probe_sql, [probe, config_sha256])
            cur.fetchone()
    return {"articles": len(db), "loaded": len(db), "replaced": replaced}


def run_load(url: str, cfg, config_sha256, cached) -> dict:
    import psycopg
    with psycopg.connect(url) as conn:  # commits on success, rolls back on error
        return load_rows(conn, cfg, config_sha256, cached)


def cmd_load(args, cfg, config_sha256, environ, run_load=run_load) -> int:
    url = environ.get("DATABASE_URL")
    problem = ingest_kb.check_database_url(url, args.allow_remote, environ)
    if problem:
        raise EmbedError(problem, EXIT_USAGE)
    cache_path = cache_path_for(args.cache_dir, config_sha256)
    journal_path = cache_path.with_name(cache_path.name + ".journal")
    if journal_path.exists():
        raise EmbedError("an interrupted embed run left a journal; finish embed first",
                         EXIT_USAGE)
    if not cache_path.exists():
        raise EmbedError(f"cache not found: {cache_path}", EXIT_USAGE)
    raw = cache_path.read_bytes()
    cached = read_cache(cache_path, cfg, config_sha256)
    print(f"cache {cache_path} sha256={sha256_hex(raw)} rows={len(cached)}")
    import psycopg
    try:
        c = run_load(url, cfg, config_sha256, cached)
    except psycopg.Error as exc:
        print(ingest_kb.describe_db_error(exc))
        print("rolled back")
        return EXIT_EXTERNAL
    print(f"articles {c['articles']} loaded {c['loaded']} replaced {c['replaced']}")
    print("ok")
    return EXIT_OK


# ------------------------------------------------------------------ main


def main(argv=None, environ=None, transport_factory=make_transport, run_load=run_load,
         encoding_loader=load_encoding) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    ap.add_argument("--tiktoken-dir", type=Path, default=TIKTOKEN_DIR)
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("preflight", help="zero-API plan (default)")
    p_embed = sub.add_parser("embed", help="paid: embed cache misses")
    p_embed.add_argument("--paid", action="store_true", help="confirm paid API calls")
    p_embed.add_argument("--max-cost-usd", type=float, required=True)
    p_load = sub.add_parser("load", help="load the cache into Postgres (no API)")
    p_load.add_argument("--allow-remote", action="store_true")
    args = ap.parse_args(argv)
    command = args.command or "preflight"
    try:
        cfg, config_sha256 = load_config(args.config)
        if command == "load":
            return cmd_load(args, cfg, config_sha256, environ, run_load)
        enc = encoding_loader(args.tiktoken_dir, cfg["encoding_file_sha256"])
        if command == "embed":
            return cmd_embed(args, cfg, config_sha256, enc, environ, transport_factory)
        return cmd_preflight(args, cfg, config_sha256, enc)
    except EmbedError as exc:
        print(f"error: {exc}")
        return exc.code


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
