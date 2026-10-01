import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { format } from "node:util";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  decodeEmbedding,
  EMBED_TIMEOUT_MS,
  EMBEDDINGS_URL,
  EmbeddingError,
  embedQuery,
  type EmbedEnv,
  type FetchFn,
} from "@/lib/embeddings/openai";
import { errorSummary } from "@/lib/safe-error";
import { SEARCH_DEPTH } from "@/lib/search/config";
import {
  handleSearch,
  type SearchEnv,
  type SearchFn,
} from "@/lib/search/handler";
import {
  makeSemanticSearch,
  SEMANTIC_SQL,
  toVectorLiteral,
  type QueryFn,
  type SemanticDeps,
} from "@/lib/search/semantic";
import {
  canonicalJson,
  getSemanticConfig,
  loadSemanticConfig,
  SemanticConfigError,
} from "@/lib/search/semantic-config";
import type { SearchRow } from "@/lib/search/types";

// Offline. tests/web/setup.ts blocks real fetch and clears OPENAI_API_KEY; every
// test that needs a key passes a fake one in an explicit env object.

const ROOT = process.cwd();
const CONFIG_FILE = join(ROOT, "config", "embedding", "article-v1.json");
// Printed by scripts/embed_kb.py preflight for the same file (cross-language pin).
const PYTHON_CONFIG_SHA256 =
  "051e13da32f484a11e0e87188a71de984563e93a5b3e1495db6da4cd0cb99940";

const FAKE_KEY = "sk-test-FAKE-0000";
const KEY_ENV: EmbedEnv = { OPENAI_API_KEY: FAKE_KEY };
const SENTINEL = "kbf-sentinel-7f3a9c";
const SENTINEL_CODE = "KBF_SENTINEL_7F3A9C";
const QUERY = `my laptop ${SENTINEL} will not connect`;
const INVALID = { error: "Invalid request" };
const FAILED = { error: "Search failed" };
const EVAL_ON: SearchEnv = { EVAL_MODES_ENABLED: "true" };

const cfg = getSemanticConfig();
const DIMS = cfg.dimensions;

// ------------------------------------------------------------------ helpers

function unitVector(index: number, dims = DIMS): number[] {
  const v = new Array<number>(dims).fill(0);
  v[index] = 1;
  return v;
}

function toB64(values: number[], littleEndian = true): string {
  const buf = Buffer.alloc(values.length * 4);
  values.forEach((x, i) =>
    littleEndian ? buf.writeFloatLE(x, i * 4) : buf.writeFloatBE(x, i * 4),
  );
  return buf.toString("base64");
}

function okBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    object: "list",
    model: cfg.model,
    data: [{ object: "embedding", index: 0, embedding: toB64(unitVector(3)) }],
    usage: { prompt_tokens: 5, total_tokens: 5 },
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fakeFetch(respond: () => Promise<Response> | Response) {
  return vi.fn<FetchFn>(async () => respond());
}

function row(rank: number, id: string, score: number): SearchRow {
  return {
    rank,
    source_article_id: id,
    title: `Title ${id}`,
    url: `https://example.test/kb/${id}`,
    category: null,
    score,
  };
}

function fakeQuery(rows: SearchRow[] = []) {
  return vi.fn<QueryFn>(async () => ({ rows }));
}

function semanticWith(
  fetchFn: FetchFn,
  env: EmbedEnv = KEY_ENV,
  query: QueryFn = fakeQuery(),
): SearchFn {
  return makeSemanticSearch({
    embed: (text, config) => embedQuery(text, config, { env, fetchFn }),
    query,
    config: getSemanticConfig,
  });
}

const lexicalNeverCalled = vi.fn<SearchFn>(async () => {
  throw new Error("lexical retriever called");
});

function jsonRequest(body: unknown): Request {
  return new Request("http://localhost/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function runSemantic(semantic: SearchFn, env: SearchEnv = EVAL_ON, query = QUERY) {
  const response = await handleSearch(jsonRequest({ query, mode: "semantic" }), {
    retrievers: { lexical: lexicalNeverCalled, semantic },
    env,
  });
  return { status: response.status, text: await response.text() };
}

function captureOutput(): string[] {
  const captured: string[] = [];
  const methods = ["log", "info", "warn", "error", "debug", "trace"] as const;
  for (const method of methods) {
    vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
      captured.push(format(...args));
    });
  }
  const write = ((chunk: string | Uint8Array) => {
    captured.push(typeof chunk === "string" ? chunk : Buffer.from(chunk).toString());
    return true;
  }) as typeof process.stdout.write;
  vi.spyOn(process.stdout, "write").mockImplementation(write);
  vi.spyOn(process.stderr, "write").mockImplementation(write);
  return captured;
}

afterEach(() => {
  expect(lexicalNeverCalled).not.toHaveBeenCalled();
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------ config

describe("semantic config comes from config/embedding/article-v1.json", () => {
  const bytes = readFileSync(CONFIG_FILE);
  const parsed = JSON.parse(bytes.toString("ascii")) as Record<string, unknown>;

  it("canonical re-serialization equals the file byte for byte", () => {
    expect(canonicalJson(parsed)).toBe(bytes.toString("ascii"));
  });

  it("sha256 equals the file's sha256 and the value Python computed", () => {
    const fileSha = createHash("sha256").update(bytes).digest("hex");
    expect(cfg.sha256).toBe(fileSha);
    expect(cfg.sha256).toBe(PYTHON_CONFIG_SHA256);
  });

  it("model and dimensions are the file's values", () => {
    expect(cfg.model).toBe(parsed.model);
    expect(cfg.dimensions).toBe(parsed.dimensions);
  });

  it("canonicalJson matches Python json.dumps(sort_keys, compact, ensure_ascii)", () => {
    const input = JSON.parse(
      String.raw`{"b": "\u00e9\n\ud83d\ude00\u007f/\"\\t\u0001", "a": [1, true, null, {"z": 0, "y": -2}], "c": ""}`,
    );
    expect(canonicalJson(input)).toBe(
      String.raw`{"a":[1,true,null,{"y":-2,"z":0}],"b":"\u00e9\n\ud83d\ude00\u007f/\"\\t\u0001","c":""}`,
    );
  });

  it("rejects configs the query side does not implement", () => {
    const bad: Record<string, unknown>[] = [
      { ...parsed, query_input: "templated-query-v2" },
      { ...parsed, response_encoding: "float" },
      { ...parsed, dimensions: 1536.5 },
      { ...parsed, dimensions: "1536" },
      { ...parsed, model: "" },
      { ...parsed, model: undefined },
    ];
    for (const raw of bad) {
      expect(() => loadSemanticConfig(raw)).toThrow(SemanticConfigError);
    }
    expect(() => loadSemanticConfig([])).toThrow(SemanticConfigError);
    expect(() => loadSemanticConfig(null)).toThrow(SemanticConfigError);
  });

  it("an invalid config fails the semantic request loudly, before any fetch", async () => {
    const fetchFn = fakeFetch(() => jsonResponse(okBody()));
    const semantic = makeSemanticSearch({
      embed: (text, config) => embedQuery(text, config, { env: KEY_ENV, fetchFn }),
      query: fakeQuery(),
      config: () => loadSemanticConfig({ ...parsed, response_encoding: "float" }),
    });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await runSemantic(semantic);
    expect(result).toEqual({ status: 500, text: JSON.stringify(FAILED) });
    expect(errors).toHaveBeenCalledWith(
      "search failed: SemanticConfigError SEMANTIC_CONFIG_INVALID",
    );
    expect(fetchFn).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------ dispatch

describe("dispatch and gating", () => {
  function fakes() {
    return {
      lexical: vi.fn<SearchFn>(async () => [row(1, "11", 0.5)]),
      semantic: vi.fn<SearchFn>(async () => [row(1, "22", 0.75)]),
    };
  }

  async function post(mode: string, env: SearchEnv, f = fakes()) {
    const response = await handleSearch(jsonRequest({ query: "wifi", mode }), {
      retrievers: f,
      env,
    });
    return { f, status: response.status, body: await response.json() };
  }

  it("a semantic request calls only the semantic retriever", async () => {
    const { f, status, body } = await post("semantic", EVAL_ON);
    expect(status).toBe(200);
    expect(f.semantic).toHaveBeenCalledTimes(1);
    expect(f.semantic).toHaveBeenCalledWith("wifi", SEARCH_DEPTH);
    expect(f.lexical).not.toHaveBeenCalled();
    expect(body).toEqual({
      mode: "semantic",
      results: [
        { rank: 1, sourceArticleId: "22", title: "Title 22",
          url: "https://example.test/kb/22", score: 0.75 },
      ],
    });
  });

  it("a lexical request calls only the lexical retriever, gate on or off", async () => {
    for (const env of [{}, EVAL_ON]) {
      const { f, status, body } = await post("lexical", env);
      expect(status).toBe(200);
      expect(body.mode).toBe("lexical");
      expect(f.lexical).toHaveBeenCalledTimes(1);
      expect(f.semantic).not.toHaveBeenCalled();
    }
  });

  it.each([
    ["unset", {}],
    ['""', { EVAL_MODES_ENABLED: "" }],
    ['"false"', { EVAL_MODES_ENABLED: "false" }],
    ['"1"', { EVAL_MODES_ENABLED: "1" }],
    ['"TRUE"', { EVAL_MODES_ENABLED: "TRUE" }],
    ['" true"', { EVAL_MODES_ENABLED: " true" }],
  ] as [string, SearchEnv][])(
    "EVAL_MODES_ENABLED %s: semantic is rejected with 400 and nothing is called",
    async (_label, env) => {
      const { f, status, body } = await post("semantic", env);
      expect(status).toBe(400);
      expect(body).toEqual(INVALID);
      expect(f.semantic).not.toHaveBeenCalled();
      expect(f.lexical).not.toHaveBeenCalled();
    },
  );

  it("DEFAULT_SEARCH_MODE=semantic without the gate allows semantic and rejects lexical", async () => {
    const env = { DEFAULT_SEARCH_MODE: "semantic" };
    expect((await post("semantic", env)).status).toBe(200);
    const lexical = await post("lexical", env);
    expect(lexical.status).toBe(400);
    expect(lexical.f.lexical).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------ OpenAI client

describe("embedQuery", () => {
  it("a missing key fails with OPENAI_KEY_MISSING before any fetch", async () => {
    for (const env of [{}, { OPENAI_API_KEY: "" }] as EmbedEnv[]) {
      const fetchFn = fakeFetch(() => jsonResponse(okBody()));
      await expect(embedQuery(QUERY, cfg, { env, fetchFn })).rejects.toMatchObject({
        name: "EmbeddingError",
        code: "OPENAI_KEY_MISSING",
      });
      expect(fetchFn).not.toHaveBeenCalled();
    }
  });

  it("reads process.env at call time; the test setup leaves no key there", async () => {
    const globalFetch = vi.spyOn(globalThis, "fetch");
    expect(process.env.OPENAI_API_KEY).toBeUndefined();
    await expect(embedQuery(QUERY, cfg)).rejects.toMatchObject({
      code: "OPENAI_KEY_MISSING",
    });
    expect(globalFetch).not.toHaveBeenCalled();
  });

  it("the key is read when the request runs, not when the retriever is built", async () => {
    const env: EmbedEnv = {};
    const fetchFn = fakeFetch(() => jsonResponse(okBody()));
    const semantic = semanticWith(fetchFn, env, fakeQuery([row(1, "1", 1)]));
    env.OPENAI_API_KEY = FAKE_KEY;
    await expect(semantic(QUERY, SEARCH_DEPTH)).resolves.toHaveLength(1);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it("sends exactly one request of the documented shape", async () => {
    const fetchFn = fakeFetch(() => jsonResponse(okBody()));
    const vector = await embedQuery(QUERY, cfg, { env: KEY_ENV, fetchFn });
    expect(vector).toEqual(unitVector(3));
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe(EMBEDDINGS_URL);
    expect(url).not.toContain(SENTINEL);
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({
      Authorization: `Bearer ${FAKE_KEY}`,
      "Content-Type": "application/json",
    });
    expect(JSON.parse(init.body as string)).toEqual({
      model: cfg.model,
      input: QUERY,
      dimensions: cfg.dimensions,
      encoding_format: "base64",
    });
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(init.cache).toBe("no-store");
    expect(init.redirect).toBe("error");
  });

  it("times out well inside run_eval.py's 30 s request timeout", () => {
    expect(EMBED_TIMEOUT_MS).toBe(10_000);
  });
});

describe("decodeEmbedding (explicit little-endian float32)", () => {
  it("reads little-endian values", () => {
    const v = new Array<number>(DIMS).fill(0);
    v[0] = 0.6;
    v[1] = 0.8;
    const got = decodeEmbedding(toB64(v), DIMS);
    expect(got[0]).toBe(Math.fround(0.6));
    expect(got[1]).toBe(Math.fround(0.8));
    expect(got).toHaveLength(DIMS);
  });

  it("does not accept the same values written big-endian", () => {
    const v = new Array<number>(DIMS).fill(0);
    v[0] = 0.6;
    v[1] = 0.8;
    expect(() => decodeEmbedding(toB64(v, false), DIMS)).toThrow(EmbeddingError);
  });

  it("requires 6,144 bytes, finite values, unit norm and strict base64", () => {
    expect(DIMS * 4).toBe(6144);
    const nan = unitVector(0);
    nan[5] = Number.NaN;
    const inf = unitVector(0);
    inf[5] = Number.POSITIVE_INFINITY;
    const bad: unknown[] = [
      Buffer.alloc(6140).toString("base64"),
      Buffer.alloc(6148).toString("base64"),
      toB64(nan),
      toB64(inf),
      toB64(new Array<number>(DIMS).fill(0)),
      toB64(unitVector(0).map((x) => x * 2)),
      toB64(unitVector(0)).slice(0, 100) + "\n" + toB64(unitVector(0)).slice(100),
      "!!!!",
      unitVector(0),
      null,
    ];
    for (const value of bad) {
      expect(() => decodeEmbedding(value, DIMS)).toThrow(EmbeddingError);
    }
  });
});

// ------------------------------------------------------------------ privacy

describe("query text never reaches logs or responses on any OpenAI error path", () => {
  const cases: [string, FetchFn | null, string][] = [
    ["key missing", null, "OPENAI_KEY_MISSING"],
    ["HTTP 429 echoing the query", fakeFetch(() => jsonResponse({ error: QUERY }, 429)),
      "OPENAI_HTTP_429"],
    ["HTTP 400 echoing the query", fakeFetch(() => jsonResponse({ error: QUERY }, 400)),
      "OPENAI_HTTP_4XX"],
    ["HTTP 401", fakeFetch(() => jsonResponse({ error: SENTINEL }, 401)), "OPENAI_HTTP_4XX"],
    ["HTTP 500 echoing the query", fakeFetch(() => jsonResponse({ error: QUERY }, 500)),
      "OPENAI_HTTP_5XX"],
    ["HTTP 503", fakeFetch(() => new Response(SENTINEL, { status: 503 })), "OPENAI_HTTP_5XX"],
    ["HTTP 302", fakeFetch(() => new Response(null, { status: 302 })), "OPENAI_HTTP_OTHER"],
    ["network error carrying the query",
      vi.fn<FetchFn>(async () => { throw new TypeError(`fetch failed: ${QUERY}`); }),
      "OPENAI_NETWORK"],
    ["timeout carrying the query",
      vi.fn<FetchFn>(async () => { throw new DOMException(QUERY, "TimeoutError"); }),
      "OPENAI_TIMEOUT"],
    ["abort carrying the query",
      vi.fn<FetchFn>(async () => { throw new DOMException(QUERY, "AbortError"); }),
      "OPENAI_TIMEOUT"],
    ["200 that is not JSON", fakeFetch(() => new Response(`<html>${QUERY}</html>`)),
      "OPENAI_BAD_RESPONSE"],
    ["200 with JSON null", fakeFetch(() => jsonResponse(null)), "OPENAI_BAD_RESPONSE"],
    ["200 with another model", fakeFetch(() => jsonResponse(okBody({ model: QUERY }))),
      "OPENAI_BAD_RESPONSE"],
    ["200 with two items", fakeFetch(() => jsonResponse(okBody({
      data: [{ index: 0, embedding: toB64(unitVector(0)) },
             { index: 1, embedding: toB64(unitVector(1)) }] }))), "OPENAI_BAD_RESPONSE"],
    ["200 with index 1", fakeFetch(() => jsonResponse(okBody({
      data: [{ index: 1, embedding: toB64(unitVector(0)) }] }))), "OPENAI_BAD_RESPONSE"],
    ["200 with a short vector", fakeFetch(() => jsonResponse(okBody({
      data: [{ index: 0, embedding: toB64(unitVector(0, 1535)) }] }))), "OPENAI_BAD_RESPONSE"],
    ["200 with the query as the embedding", fakeFetch(() => jsonResponse(okBody({
      data: [{ index: 0, embedding: QUERY }] }))), "OPENAI_BAD_RESPONSE"],
  ];

  it.each(cases)("%s", async (_name, fetchFn, code) => {
    const captured = captureOutput();
    const semantic = semanticWith(fetchFn ?? fakeFetch(() => jsonResponse(okBody())),
      fetchFn ? KEY_ENV : {});
    const result = await runSemantic(semantic);
    vi.restoreAllMocks();

    expect(result).toEqual({ status: 500, text: JSON.stringify(FAILED) });
    expect(captured).toEqual([`search failed: EmbeddingError ${code}`]);
    for (const text of [result.text, ...captured]) {
      expect(text).not.toContain(SENTINEL);
      expect(text).not.toContain(SENTINEL_CODE);
      expect(text).not.toContain(FAKE_KEY);
    }
  });

  const thrown: [string, unknown, string][] = [
    ["error message holding the query", new Error(`boom ${QUERY}`),
      "search failed: EmbeddingError OPENAI_UNEXPECTED"],
    ["error code holding the query", { code: SENTINEL_CODE, message: QUERY },
      "search failed: EmbeddingError OPENAI_UNEXPECTED"],
    ["EmbeddingError with a forged code",
      Object.assign(new EmbeddingError("OPENAI_HTTP_429"), { code: SENTINEL_CODE }),
      "search failed: EmbeddingError OPENAI_UNEXPECTED"],
    ["EmbeddingError with a known code and the query as message",
      Object.assign(new EmbeddingError("OPENAI_HTTP_429"), { message: QUERY }),
      "search failed: EmbeddingError OPENAI_HTTP_429"],
  ];

  it.each(thrown)("embedder throwing %s is wrapped to a fixed code", async (_n, err, line) => {
    const captured = captureOutput();
    const query = fakeQuery();
    const semantic = makeSemanticSearch({
      embed: async () => { throw err; },
      query,
      config: getSemanticConfig,
    });
    const result = await runSemantic(semantic);
    vi.restoreAllMocks();

    expect(result).toEqual({ status: 500, text: JSON.stringify(FAILED) });
    expect(captured).toEqual([line]);
    expect(query).not.toHaveBeenCalled();
    for (const text of [result.text, ...captured]) {
      expect(text).not.toContain(SENTINEL);
      expect(text).not.toContain(SENTINEL_CODE);
    }
  });

  it("without the wrap, errorSummary would have logged the forged code", () => {
    expect(errorSummary({ code: SENTINEL_CODE })).toBe(`object ${SENTINEL_CODE}`);
  });

  it("a database error after embedding logs only class and SQLSTATE", async () => {
    class DatabaseError extends Error {
      constructor(message: string, readonly code: string) {
        super(message);
      }
    }
    const captured = captureOutput();
    const semantic = semanticWith(
      fakeFetch(() => jsonResponse(okBody())),
      KEY_ENV,
      vi.fn<QueryFn>(async () => { throw new DatabaseError(`stale ${QUERY}`, "KBF05"); }),
    );
    const result = await runSemantic(semantic);
    vi.restoreAllMocks();
    expect(result.status).toBe(500);
    expect(captured).toEqual(["search failed: DatabaseError KBF05"]);
  });

  it("an invalid vector logs VectorError and never reaches the database", async () => {
    const captured = captureOutput();
    const query = fakeQuery();
    const semantic = makeSemanticSearch({
      embed: async () => unitVector(0, DIMS - 1),
      query,
      config: getSemanticConfig,
    });
    const result = await runSemantic(semantic);
    vi.restoreAllMocks();
    expect(result.status).toBe(500);
    expect(captured).toEqual(["search failed: VectorError VECTOR_INVALID"]);
    expect(query).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------ vector and SQL

describe("vector literal and SQL parameters", () => {
  it("serializes 1536 finite values as a pgvector literal", () => {
    const v = unitVector(7);
    v[8] = -0;
    v[9] = Math.fround(1e-7);
    const lit = toVectorLiteral(v, DIMS);
    expect(lit.startsWith("[") && lit.endsWith("]")).toBe(true);
    const parts = lit.slice(1, -1).split(",");
    expect(parts).toHaveLength(DIMS);
    expect(parts[7]).toBe("1");
    expect(parts[8]).toBe("0");
    expect(parts.map(Number).map(Math.fround)).toEqual(v.map((x) => Math.fround(x) + 0));
  });

  it("round-trips float32 values exactly", () => {
    const raw = Buffer.from(Array.from({ length: DIMS * 4 }, (_, i) => (i * 37) % 251));
    const values = Array.from({ length: DIMS }, (_, i) => raw.readFloatLE(i * 4))
      .map((x) => (Number.isFinite(x) ? x : 0.5));
    const back = toVectorLiteral(values, DIMS).slice(1, -1).split(",").map(Number);
    expect(back.map(Math.fround)).toEqual(values.map(Math.fround));
    expect(back).toEqual(values);
  });

  it("rejects wrong dimensions, non-finite, non-number and all-zero vectors", () => {
    const withAt = (i: number, x: unknown) => {
      const v: unknown[] = unitVector(0);
      v[i] = x;
      return v;
    };
    const bad: unknown[][] = [
      unitVector(0, DIMS - 1),
      unitVector(0, DIMS + 1),
      withAt(3, Number.NaN),
      withAt(3, Number.POSITIVE_INFINITY),
      withAt(3, Number.NEGATIVE_INFINITY),
      withAt(3, "0.1"),
      withAt(3, null),
      new Array<number>(DIMS).fill(0),
    ];
    for (const v of bad) expect(() => toVectorLiteral(v, DIMS)).toThrow("vector invalid");
  });

  it("calls the database once, unnamed, with the literal, depth and config sha", async () => {
    const query = fakeQuery([row(1, "9", 0.9), row(2, "10", 0.8)]);
    const deps: SemanticDeps = {
      embed: async () => unitVector(4),
      query,
      config: getSemanticConfig,
    };
    const rows = await makeSemanticSearch(deps)(QUERY, SEARCH_DEPTH);
    expect(rows.map((r) => r.source_article_id)).toEqual(["9", "10"]);
    expect(query).toHaveBeenCalledTimes(1);
    const args = query.mock.calls[0];
    expect(args).toHaveLength(2);
    expect(typeof args[0]).toBe("string"); // text + values: never a named query config
    expect(args[0]).toBe(SEMANTIC_SQL);
    expect(args[1]).toEqual([toVectorLiteral(unitVector(4), DIMS), SEARCH_DEPTH, cfg.sha256]);
    expect(JSON.stringify(args)).not.toContain(SENTINEL);
  });

  it("casts the vector in the function argument", () => {
    expect(SEMANTIC_SQL).toBe(
      "select rank, source_article_id, title, url, category, score " +
        "from kb_search_semantic($1::text::extensions.vector, $2::int, $3::text)",
    );
  });

  it("a successful semantic request returns mapped rows", async () => {
    const semantic = semanticWith(
      fakeFetch(() => jsonResponse(okBody())),
      KEY_ENV,
      fakeQuery([row(1, "4778", 0.61)]),
    );
    const result = await runSemantic(semantic, EVAL_ON, "wifi");
    expect(result.status).toBe(200);
    expect(JSON.parse(result.text)).toEqual({
      mode: "semantic",
      results: [{ rank: 1, sourceArticleId: "4778", title: "Title 4778",
        url: "https://example.test/kb/4778", score: 0.61 }],
    });
  });
});

// ------------------------------------------------------------------ credential isolation

describe("credential isolation", () => {
  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return sourceFiles(full);
      return /\.(ts|tsx|js|mjs|cjs)$/.test(name) ? [full] : [];
    });
  }

  const files = [
    ...sourceFiles(join(ROOT, "app")),
    ...sourceFiles(join(ROOT, "lib")),
    join(ROOT, "next.config.ts"),
    join(ROOT, ".env.example"),
    join(ROOT, "supabase", "config.toml"),
  ];

  it("no NEXT_PUBLIC_ name exists in app, lib, next.config.ts, .env.example or supabase/config.toml", () => {
    expect(files.length).toBeGreaterThan(5);
    for (const file of files) {
      expect(readFileSync(file, "utf8"), file).not.toMatch(/NEXT_PUBLIC_[A-Za-z0-9_]/);
    }
  });

  it("OPENAI_API_KEY is read in exactly one source file, which is server-only", () => {
    const readers = files
      .filter((f) => !f.endsWith(".env.example"))
      .filter((f) => readFileSync(f, "utf8").includes("OPENAI_API_KEY"));
    expect(readers).toEqual([join(ROOT, "lib", "embeddings", "openai.ts")]);
    for (const f of ["lib/embeddings/openai.ts", "lib/search/semantic.ts",
                     "lib/search/semantic-config.ts"]) {
      expect(readFileSync(join(ROOT, f), "utf8").startsWith('import "server-only";'), f)
        .toBe(true);
    }
  });

  it("no Supabase component reads the OpenAI key", () => {
    const toml = readFileSync(join(ROOT, "supabase", "config.toml"), "utf8");
    expect(toml).not.toContain("OPENAI_API_KEY");
    // No setting (comment lines aside) mentions OpenAI at all, e.g. Studio's
    // openai_api_key = "env(...)".
    const settings = toml.split(/\r?\n/).filter((l) => !/^\s*#/.test(l));
    expect(settings.filter((l) => /openai/i.test(l))).toEqual([]);
  });

  it("the client page imports nothing from the semantic or OpenAI modules", () => {
    const page = readFileSync(join(ROOT, "app", "page.tsx"), "utf8");
    expect(page).not.toMatch(/embeddings|semantic/);
  });

  it(".env.example documents the key without a value", () => {
    const lines = readFileSync(join(ROOT, ".env.example"), "utf8").split(/\r?\n/);
    expect(lines.some((l) => /^\s*OPENAI_API_KEY\s*=/.test(l))).toBe(false);
    expect(lines.some((l) => l.includes("OPENAI_API_KEY"))).toBe(true);
  });
});
