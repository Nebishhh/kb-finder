import { format } from "node:util";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_SEARCH_MODES,
  SEARCH_DEPTH,
  SEARCH_MODES,
} from "@/lib/search/config";
import {
  handleSearch,
  isModeAllowed,
  type GateFn,
  type LimitFn,
  resolveDefaultMode,
  type SearchEnv,
  type SearchFn,
} from "@/lib/search/handler";
import type { SearchRow } from "@/lib/search/types";

// Offline tests: a fake search function stands in for the database.

const INVALID = { error: "Invalid request" };
const FAILED = { error: "Search failed" };

function jsonRequest(body: unknown): Request {
  return rawRequest(JSON.stringify(body));
}

function rawRequest(text: string): Request {
  return new Request("http://localhost/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: text,
  });
}

function fakeSearch(rows: SearchRow[] = []) {
  const depths: number[] = [];
  const search = vi.fn<SearchFn>(async (_query, depth) => {
    depths.push(depth);
    return rows;
  });
  return { search, depths };
}

function failingSearch(err: unknown) {
  return vi.fn<SearchFn>(async () => {
    throw err;
  });
}

function row(
  rank: number,
  id: string,
  score: number,
  category: string | null,
): SearchRow {
  return {
    rank,
    source_article_id: id,
    title: `Title ${id}`,
    url: `https://example.test/kb/${id}`,
    category,
    score,
  };
}

// search is the lexical retriever. The semantic retriever is a fake that throws,
// so any test here that reached it would get a 500 instead of its expected result.
const semanticNeverCalled = vi.fn<SearchFn>(async () => {
  throw new Error("semantic retriever called");
});

const lexicalV2NeverCalled = vi.fn<SearchFn>(async () => {
  throw new Error("lexical-v2 retriever called");
});

const semanticV2NeverCalled = vi.fn<SearchFn>(async () => {
  throw new Error("semantic-v2 retriever called");
});

const allow: LimitFn = async () => "ok";
const open: GateFn = () => "open";

function deps(
  search: SearchFn,
  env: SearchEnv = {},
  limit: LimitFn = allow,
  gate: GateFn = open,
) {
  return {
    retrievers: {
      lexical: search,
      semantic: semanticNeverCalled,
      "lexical-v2": lexicalV2NeverCalled,
      "semantic-v2": semanticV2NeverCalled,
    },
    gate,
    limit,
    env,
  };
}

async function run(request: Request, search: SearchFn, env: SearchEnv = {}) {
  const response = await handleSearch(request, deps(search, env));
  return { status: response.status, body: await response.json() };
}

afterEach(() => {
  expect(semanticNeverCalled).not.toHaveBeenCalled();
  expect(lexicalV2NeverCalled).not.toHaveBeenCalled();
  expect(semanticV2NeverCalled).not.toHaveBeenCalled();
  vi.restoreAllMocks();
});

describe("request validation", () => {
  const invalid: [string, Request][] = [
    ["missing query", jsonRequest({ mode: "lexical" })],
    ["empty query", jsonRequest({ query: "", mode: "lexical" })],
    ["5001 characters", jsonRequest({ query: "a".repeat(5001), mode: "lexical" })],
    ["query wrong type", jsonRequest({ query: 42, mode: "lexical" })],
    ["missing mode", jsonRequest({ query: "wifi" })],
    ["mode semantic", jsonRequest({ query: "wifi", mode: "semantic" })],
    ["invalid JSON", rawRequest('{"query": "wifi", ')],
    ["extra field limit", jsonRequest({ query: "wifi", mode: "lexical", limit: 100 })],
    ["null body", jsonRequest(null)],
    ["array body", jsonRequest(["wifi", "lexical"])],
  ];

  it.each(invalid)("%s returns 400 with a fixed body", async (_name, request) => {
    const { search } = fakeSearch();
    const result = await run(request, search);
    expect(result).toEqual({ status: 400, body: INVALID });
    expect(search).not.toHaveBeenCalled();
  });

  it("accepts a 5000 character query", async () => {
    const { search } = fakeSearch();
    const query = "a".repeat(5000);
    const result = await run(jsonRequest({ query, mode: "lexical" }), search);
    expect(result.status).toBe(200);
    expect(search).toHaveBeenCalledWith(query, SEARCH_DEPTH);
  });
});

describe("mode gating", () => {
  const modes = ["lexical", "semantic"] as const;

  it("resolves the default mode", () => {
    expect(resolveDefaultMode({})).toBe("lexical");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "" })).toBe("lexical");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "lexical" })).toBe("lexical");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "semantic" })).toBe("semantic");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "semantic" }, ["lexical"])).toBeNull();
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "bm25" })).toBeNull();
  });

  it("always allows the default mode", () => {
    expect(isModeAllowed("lexical", "lexical", {}, modes)).toBe(true);
    expect(isModeAllowed("semantic", "semantic", {}, modes)).toBe(true);
  });

  it("blocks other known modes unless EVAL_MODES_ENABLED is exactly true", () => {
    expect(isModeAllowed("semantic", "lexical", {}, modes)).toBe(false);
    for (const value of ["false", "1", "TRUE", "yes", ""]) {
      expect(
        isModeAllowed("semantic", "lexical", { EVAL_MODES_ENABLED: value }, modes),
      ).toBe(false);
    }
    expect(
      isModeAllowed("semantic", "lexical", { EVAL_MODES_ENABLED: "true" }, modes),
    ).toBe(true);
  });

  it("rejects unknown modes even with eval modes enabled", () => {
    const env = { EVAL_MODES_ENABLED: "true" };
    expect(isModeAllowed("bm25", "lexical", env, modes)).toBe(false);
    expect(isModeAllowed("bm25", "lexical", env)).toBe(false);
    expect(isModeAllowed("semantic", "lexical", env, ["lexical"])).toBe(false);
  });

  it("returns 500 when DEFAULT_SEARCH_MODE is unknown", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const { search } = fakeSearch();
    const result = await run(
      jsonRequest({ query: "wifi", mode: "lexical" }),
      search,
      { DEFAULT_SEARCH_MODE: "bogus" },
    );
    expect(result).toEqual({ status: 500, body: FAILED });
    expect(search).not.toHaveBeenCalled();
    expect(errors).toHaveBeenCalledWith(
      "search config error: invalid DEFAULT_SEARCH_MODE",
    );
  });
});

// article-v2 experiment: lexical-v2 is an eval-only mode. It is reachable only
// with EVAL_MODES_ENABLED exactly "true" and is never a valid default.
describe("lexical-v2 gating", () => {
  function v2Deps(env: SearchEnv) {
    const lexical = vi.fn<SearchFn>(async () => [row(1, "11", 0.5, null)]);
    const semantic = vi.fn<SearchFn>(async () => [row(1, "22", 0.75, null)]);
    const lexicalV2 = vi.fn<SearchFn>(async () => [row(1, "33", 0.25, null)]);
    const semanticV2 = vi.fn<SearchFn>(async () => [row(1, "44", 0.125, null)]);
    return {
      f: { lexical, semantic, "lexical-v2": lexicalV2, "semantic-v2": semanticV2 },
      deps: {
        retrievers: { lexical, semantic, "lexical-v2": lexicalV2, "semantic-v2": semanticV2 },
        gate: open,
        limit: allow,
        env,
      },
    };
  }

  async function post(mode: string, env: SearchEnv) {
    const { f, deps: d } = v2Deps(env);
    const response = await handleSearch(jsonRequest({ query: "wifi", mode }), d);
    return { f, status: response.status, body: await response.json() };
  }

  it("is a known mode but not a possible default", () => {
    expect(SEARCH_MODES).toContain("lexical-v2");
    expect(DEFAULT_SEARCH_MODES).toEqual(["lexical", "semantic"]);
  });

  it("is blocked unless EVAL_MODES_ENABLED is exactly true", async () => {
    expect(isModeAllowed("lexical-v2", "lexical", {})).toBe(false);
    expect(isModeAllowed("lexical-v2", "semantic", {})).toBe(false);
    for (const value of [undefined, "false", "1", "TRUE", "yes", "", " true"]) {
      const env: SearchEnv = value === undefined ? {} : { EVAL_MODES_ENABLED: value };
      const { f, status, body } = await post("lexical-v2", env);
      expect({ value, status, body }).toEqual({ value, status: 400, body: INVALID });
      expect(f["lexical-v2"]).not.toHaveBeenCalled();
      expect(f.lexical).not.toHaveBeenCalled();
      expect(f.semantic).not.toHaveBeenCalled();
    }
  });

  it("with EVAL_MODES_ENABLED=true calls only the lexical-v2 retriever", async () => {
    expect(isModeAllowed("lexical-v2", "lexical", { EVAL_MODES_ENABLED: "true" })).toBe(true);
    const { f, status, body } = await post("lexical-v2", { EVAL_MODES_ENABLED: "true" });
    expect(status).toBe(200);
    expect(body).toEqual({
      mode: "lexical-v2",
      results: [{ rank: 1, sourceArticleId: "33", title: "Title 33",
        url: "https://example.test/kb/33", score: 0.25 }],
    });
    expect(f["lexical-v2"]).toHaveBeenCalledTimes(1);
    expect(f["lexical-v2"]).toHaveBeenCalledWith("wifi", SEARCH_DEPTH);
    expect(f.lexical).not.toHaveBeenCalled();
    expect(f.semantic).not.toHaveBeenCalled();
  });

  it("DEFAULT_SEARCH_MODE=lexical-v2 is a config error, with or without eval modes", async () => {
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "lexical-v2" })).toBeNull();
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    for (const env of [
      { DEFAULT_SEARCH_MODE: "lexical-v2" },
      { DEFAULT_SEARCH_MODE: "lexical-v2", EVAL_MODES_ENABLED: "true" },
    ]) {
      for (const mode of ["lexical-v2", "lexical"]) {
        const { f, status, body } = await post(mode, env);
        expect({ mode, status, body }).toEqual({ mode, status: 500, body: FAILED });
        expect(f["lexical-v2"]).not.toHaveBeenCalled();
        expect(f.lexical).not.toHaveBeenCalled();
      }
    }
    expect(errors).toHaveBeenCalledWith("search config error: invalid DEFAULT_SEARCH_MODE");
  });

  it("existing defaults still resolve the same way", () => {
    expect(resolveDefaultMode({})).toBe("lexical");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "lexical" })).toBe("lexical");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "semantic" })).toBe("semantic");
  });
});

// article-v2 experiment: semantic-v2 is an eval-only mode, gated exactly like
// lexical-v2, and never a valid default.
describe("semantic-v2 gating", () => {
  function v2Deps(env: SearchEnv) {
    const lexical = vi.fn<SearchFn>(async () => [row(1, "11", 0.5, null)]);
    const semantic = vi.fn<SearchFn>(async () => [row(1, "22", 0.75, null)]);
    const lexicalV2 = vi.fn<SearchFn>(async () => [row(1, "33", 0.25, null)]);
    const semanticV2 = vi.fn<SearchFn>(async () => [row(1, "44", 0.125, null)]);
    const f = { lexical, semantic, "lexical-v2": lexicalV2, "semantic-v2": semanticV2 };
    return { f, deps: { retrievers: f, gate: open, limit: allow, env } };
  }

  async function post(mode: string, env: SearchEnv) {
    const { f, deps: d } = v2Deps(env);
    const response = await handleSearch(jsonRequest({ query: "wifi", mode }), d);
    return { f, status: response.status, body: await response.json() };
  }

  function expectNoneCalled(f: ReturnType<typeof v2Deps>["f"]) {
    for (const fn of Object.values(f)) expect(fn).not.toHaveBeenCalled();
  }

  it("is a known mode but not a possible default", () => {
    expect(SEARCH_MODES).toContain("semantic-v2");
    expect(SEARCH_MODES).toEqual(["lexical", "semantic", "lexical-v2", "semantic-v2"]);
    expect(DEFAULT_SEARCH_MODES).toEqual(["lexical", "semantic"]);
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "semantic-v2" })).toBeNull();
    expect(
      resolveDefaultMode({ DEFAULT_SEARCH_MODE: "semantic-v2", EVAL_MODES_ENABLED: "true" }),
    ).toBeNull();
  });

  it("is blocked unless EVAL_MODES_ENABLED is exactly true, whatever the default", async () => {
    for (const defaultMode of [undefined, "lexical", "semantic"]) {
      expect(isModeAllowed("semantic-v2", defaultMode ?? "lexical", {})).toBe(false);
      for (const value of [undefined, "false", "1", "TRUE", "yes", "", " true", "true "]) {
        const env: SearchEnv = {
          ...(defaultMode === undefined ? {} : { DEFAULT_SEARCH_MODE: defaultMode }),
          ...(value === undefined ? {} : { EVAL_MODES_ENABLED: value }),
        };
        const { f, status, body } = await post("semantic-v2", env);
        expect({ defaultMode, value, status, body })
          .toEqual({ defaultMode, value, status: 400, body: INVALID });
        expectNoneCalled(f);
      }
    }
  });

  it("with EVAL_MODES_ENABLED=true calls only the semantic-v2 retriever", async () => {
    for (const defaultMode of [undefined, "lexical", "semantic"]) {
      const env: SearchEnv = {
        EVAL_MODES_ENABLED: "true",
        ...(defaultMode === undefined ? {} : { DEFAULT_SEARCH_MODE: defaultMode }),
      };
      expect(isModeAllowed("semantic-v2", defaultMode ?? "lexical", env)).toBe(true);
      const { f, status, body } = await post("semantic-v2", env);
      expect(status).toBe(200);
      expect(body).toEqual({
        mode: "semantic-v2",
        results: [{ rank: 1, sourceArticleId: "44", title: "Title 44",
          url: "https://example.test/kb/44", score: 0.125 }],
      });
      expect(f["semantic-v2"]).toHaveBeenCalledTimes(1);
      expect(f["semantic-v2"]).toHaveBeenCalledWith("wifi", SEARCH_DEPTH);
      expect(f.lexical).not.toHaveBeenCalled();
      expect(f.semantic).not.toHaveBeenCalled();
      expect(f["lexical-v2"]).not.toHaveBeenCalled();
    }
  });

  it("DEFAULT_SEARCH_MODE=semantic-v2 is a config error, with or without eval modes", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    for (const env of [
      { DEFAULT_SEARCH_MODE: "semantic-v2" },
      { DEFAULT_SEARCH_MODE: "semantic-v2", EVAL_MODES_ENABLED: "true" },
    ]) {
      for (const mode of ["semantic-v2", "semantic", "lexical", "lexical-v2"]) {
        const { f, status, body } = await post(mode, env);
        expect({ mode, status, body }).toEqual({ mode, status: 500, body: FAILED });
        expectNoneCalled(f);
      }
    }
    expect(errors).toHaveBeenCalledWith("search config error: invalid DEFAULT_SEARCH_MODE");
  });
});

describe("response mapping", () => {
  it("maps rows 1:1 in database order, omitting null categories", async () => {
    // Scores deliberately out of order: the handler must not re-sort.
    const rows = [
      row(1, "4778", 0.1, "Email"),
      row(2, "12", 0.9, null),
      row(3, "305", 0.5, "Network"),
    ];
    const { search } = fakeSearch(rows);
    const response = await handleSearch(
      jsonRequest({ query: "wifi", mode: "lexical" }),
      deps(search),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    const body = await response.json();
    expect(body).toEqual({
      mode: "lexical",
      results: [
        {
          rank: 1,
          sourceArticleId: "4778",
          title: "Title 4778",
          url: "https://example.test/kb/4778",
          category: "Email",
          score: 0.1,
        },
        {
          rank: 2,
          sourceArticleId: "12",
          title: "Title 12",
          url: "https://example.test/kb/12",
          score: 0.9,
        },
        {
          rank: 3,
          sourceArticleId: "305",
          title: "Title 305",
          url: "https://example.test/kb/305",
          category: "Network",
          score: 0.5,
        },
      ],
    });
    expect("category" in body.results[1]).toBe(false);
    expect(Object.keys(body.results[0])).toEqual([
      "rank",
      "sourceArticleId",
      "title",
      "url",
      "category",
      "score",
    ]);
  });

  it("returns 200 with [] when there are no results", async () => {
    const { search } = fakeSearch([]);
    const result = await run(jsonRequest({ query: "   ", mode: "lexical" }), search);
    expect(result).toEqual({ status: 200, body: { mode: "lexical", results: [] } });
  });
});

describe("retrieval depth", () => {
  it("is the constant 50", () => {
    expect(SEARCH_DEPTH).toBe(50);
  });

  it("passes 50 to search on every call", async () => {
    const { search, depths } = fakeSearch();
    for (const query of ["wifi", "a".repeat(5000), "   ", "the and of"]) {
      await run(jsonRequest({ query, mode: "lexical" }), search);
    }
    expect(depths).toEqual([50, 50, 50, 50]);
  });
});

describe("database errors", () => {
  it("return 500 with a fixed body and log only class and code", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    class DatabaseError extends Error {
      code = "42601";
    }
    const search = failingSearch(new DatabaseError("syntax error near wifi"));
    const result = await run(jsonRequest({ query: "wifi", mode: "lexical" }), search);
    expect(result).toEqual({ status: 500, body: FAILED });
    expect(errors).toHaveBeenCalledTimes(1);
    expect(errors).toHaveBeenCalledWith("search failed: DatabaseError 42601");
  });
});

describe("query text never leaks", () => {
  const SENTINEL = "kbf-sentinel-7f3a9c";

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

  class DatabaseError extends Error {
    constructor(
      message: string,
      readonly code: string,
    ) {
      super(message);
    }
  }

  const query = `my laptop ${SENTINEL} will not connect`;

  const cases: [string, Request, SearchFn, SearchEnv, LimitFn?, GateFn?][] = [
    [
      "success",
      jsonRequest({ query, mode: "lexical" }),
      fakeSearch([row(1, "4778", 0.5, "Email")]).search,
      {},
    ],
    [
      "database error echoing the query",
      jsonRequest({ query, mode: "lexical" }),
      failingSearch(new DatabaseError(`syntax error in tsquery: "${query}"`, "42601")),
      {},
    ],
    [
      "database error with the query as its code",
      jsonRequest({ query, mode: "lexical" }),
      failingSearch(new DatabaseError(query, query)),
      {},
    ],
    [
      "too-long query",
      jsonRequest({ query: SENTINEL + "x".repeat(5001), mode: "lexical" }),
      fakeSearch().search,
      {},
    ],
    [
      "bad mode",
      jsonRequest({ query, mode: "semantic" }),
      fakeSearch().search,
      {},
    ],
    [
      "sentinel as the mode",
      jsonRequest({ query, mode: SENTINEL }),
      fakeSearch().search,
      {},
    ],
    [
      "extra field",
      jsonRequest({ query, mode: "lexical", limit: 100 }),
      fakeSearch().search,
      {},
    ],
    [
      "extra field holding the sentinel",
      jsonRequest({ query, mode: "lexical", note: SENTINEL }),
      fakeSearch().search,
      {},
    ],
    [
      "invalid JSON",
      rawRequest(`{"query": "${SENTINEL}`),
      fakeSearch().search,
      {},
    ],
    [
      "unknown DEFAULT_SEARCH_MODE",
      jsonRequest({ query, mode: "lexical" }),
      fakeSearch().search,
      { DEFAULT_SEARCH_MODE: "bogus" },
    ],
    [
      "body over the byte cap (413, streamed)",
      jsonRequest({ query: `${query} ${SENTINEL.repeat(2000)}`, mode: "lexical" }),
      fakeSearch().search,
      {},
    ],
    [
      "Content-Length over the byte cap (413)",
      new Request("http://localhost/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": "40000" },
        body: JSON.stringify({ query, mode: "lexical" }),
      }),
      fakeSearch().search,
      {},
    ],
    [
      "over the daily cap (429)",
      jsonRequest({ query, mode: "lexical" }),
      fakeSearch().search,
      {},
      async () => "daily_cap",
    ],
    [
      "limiter error echoing the query (500)",
      jsonRequest({ query, mode: "lexical" }),
      fakeSearch().search,
      {},
      async () => {
        throw new DatabaseError(query, query);
      },
    ],
    [
      "no gate cookie (401)",
      jsonRequest({ query, mode: "lexical" }),
      fakeSearch().search,
      {},
      allow,
      () => "denied",
    ],
    [
      "gate misconfigured (500)",
      jsonRequest({ query, mode: "lexical" }),
      fakeSearch().search,
      {},
      allow,
      () => "misconfigured",
    ],
  ];

  it.each(cases)("%s", async (_name, request, search, env, limit, gate) => {
    const captured = captureOutput();
    const response = await handleSearch(request, deps(search, env, limit, gate));
    const text = await response.text();
    vi.restoreAllMocks();

    expect(text).not.toContain(SENTINEL);
    for (const line of captured) expect(line).not.toContain(SENTINEL);
  });

  it("capture works: the database error path is logged and seen", async () => {
    const captured = captureOutput();
    await handleSearch(
      jsonRequest({ query, mode: "lexical" }),
      deps(failingSearch(new DatabaseError(query, query))),
    );
    vi.restoreAllMocks();

    expect(captured).toContain("search failed: DatabaseError -");
  });
});
