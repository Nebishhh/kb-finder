import { readFileSync } from "node:fs";
import { join } from "node:path";
import { format } from "node:util";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_SEARCH_MODES,
  HYBRID_RRF_K,
  SEARCH_DEPTH,
  SEARCH_MODES,
} from "@/lib/search/config";
import {
  handleSearch,
  isModeAllowed,
  resolveDefaultMode,
  type SearchEnv,
  type SearchFn,
} from "@/lib/search/handler";
import { fuseRrf, HybridLegError, makeHybridSearch } from "@/lib/search/hybrid";
import type { SearchRow } from "@/lib/search/types";

// Hybrid experiment hybrid-rrf-v1 (DECISIONS.md, 2026-11-15). Offline: both
// legs are fakes. No database, no OpenAI client, and only made-up queries.

const INVALID = { error: "Invalid request" };
const FAILED = { error: "Search failed" };
const EVAL_ON: SearchEnv = { EVAL_MODES_ENABLED: "true" };
const K = 60;
const MISSING = SEARCH_DEPTH + 1; // 51

function row(rank: number, id: string, score = 0.5): SearchRow {
  return {
    rank,
    source_article_id: id,
    title: `Title ${id}`,
    url: `https://example.test/kb/${id}`,
    category: null,
    score,
  };
}

// A leg from article IDs in rank order.
function leg(...ids: string[]): SearchRow[] {
  return ids.map((id, index) => row(index + 1, id));
}

// IDs prefix1..prefixN.
function ids(prefix: string, n: number): string[] {
  return Array.from({ length: n }, (_, i) => `${prefix}${i + 1}`);
}

function rrf(lexicalRank: number, semanticRank: number): number {
  return 1 / (K + lexicalRank) + 1 / (K + semanticRank);
}

function order(rows: SearchRow[]): string[] {
  return rows.map((r) => r.source_article_id);
}

function jsonRequest(body: unknown): Request {
  return new Request("http://localhost/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("frozen constants", () => {
  it("k is 60 and a missing article counts as rank 51", () => {
    expect(HYBRID_RRF_K).toBe(60);
    expect(SEARCH_DEPTH).toBe(50);
    // "9" is only in the semantic leg: its lexical rank is 51.
    const fused = fuseRrf(leg("1"), leg("9"), SEARCH_DEPTH);
    expect(fused.find((r) => r.source_article_id === "9")?.score).toBe(1 / 111 + 1 / 61);
  });
});

describe("fuseRrf", () => {
  it("scores each article 1/(60 + lexical rank) + 1/(60 + semantic rank)", () => {
    // 5: lexical 1, semantic 3. 7: lexical 2, semantic 1. 8: lexical 3, semantic 2.
    const fused = fuseRrf(leg("5", "7", "8"), leg("7", "8", "5"), SEARCH_DEPTH);
    expect(fused).toEqual([
      { ...row(1, "7"), score: rrf(2, 1) },
      { ...row(2, "5"), score: rrf(1, 3) },
      { ...row(3, "8"), score: rrf(3, 2) },
    ]);
  });

  it("returns ranks 1..n and descending scores", () => {
    const fused = fuseRrf(leg(...ids("a", 50)), leg(...ids("b", 50)), SEARCH_DEPTH);
    expect(fused.map((r) => r.rank)).toEqual(Array.from({ length: 50 }, (_, i) => i + 1));
    for (let i = 1; i < fused.length; i += 1) {
      expect(fused[i].score).toBeLessThanOrEqual(fused[i - 1].score);
    }
  });

  it("keeps the union when it fits and cuts it at depth when it does not", () => {
    expect(order(fuseRrf(leg("1", "2"), leg("3", "2"), SEARCH_DEPTH)).sort()).toEqual([
      "1", "2", "3",
    ]);
    // Two disjoint legs of 50: 100 candidates, 50 returned.
    const fused = fuseRrf(leg(...ids("a", 50)), leg(...ids("b", 50)), SEARCH_DEPTH);
    expect(fused).toHaveLength(50);
    expect(new Set(order(fused)).size).toBe(50);
  });

  it("an empty lexical leg gives the semantic order", () => {
    const semantic = leg("4", "2", "9");
    const fused = fuseRrf([], semantic, SEARCH_DEPTH);
    expect(order(fused)).toEqual(["4", "2", "9"]);
    expect(fused[0].score).toBe(rrf(MISSING, 1));
  });

  it("an empty semantic leg gives the lexical order, and two empty legs give []", () => {
    expect(order(fuseRrf(leg("4", "2", "9"), [], SEARCH_DEPTH))).toEqual(["4", "2", "9"]);
    expect(fuseRrf([], [], SEARCH_DEPTH)).toEqual([]);
  });

  it("a semantic-only rank 1 is not buried under articles found by both legs", () => {
    // "solo" is semantic rank 1 and has no lexical hit. x1..x48 are in both
    // legs (lexical i, semantic i + 1). Plain RRF (missing = 0) would put all
    // 48 of them above "solo", because even 2/110 is more than 1/61.
    const shared = ids("x", 48);
    const fused = fuseRrf(leg(...shared, "tail"), leg("solo", ...shared), SEARCH_DEPTH);
    const solo = fused.find((r) => r.source_article_id === "solo");
    expect(2 / 110).toBeGreaterThan(1 / 61); // the artifact the rule removes
    expect(solo?.score).toBe(rrf(MISSING, 1));
    expect(solo?.score).toBeGreaterThan(rrf(50, 50));
    // Only x1..x18 score higher: 1/(60 + i) + 1/(61 + i) > 1/111 + 1/61 for i <= 18.
    expect(solo?.rank).toBe(19);
    expect(order(fused).slice(0, 18)).toEqual(shared.slice(0, 18));
  });

  it("the score is continuous at the cutoff: rank 50 is just above missing", () => {
    // "in" is lexical rank 50, "out" is not in the lexical leg; both are
    // semantic-adjacent (ranks 1 and 2), so only the lexical term separates them.
    const lexical = leg(...ids("l", 49), "in");
    const fused = fuseRrf(lexical, leg("out", "in"), SEARCH_DEPTH);
    const scoreOf = (id: string) => fused.find((r) => r.source_article_id === id)!.score;
    expect(scoreOf("in")).toBe(rrf(50, 2));
    expect(scoreOf("out")).toBe(rrf(MISSING, 1));
    expect(1 / 110 - 1 / 111).toBeLessThan(0.0001);
  });

  it("an exact tie goes to the better semantic rank", () => {
    // "x": lexical 1, semantic 2. "y": lexical 2, semantic 1. Equal scores.
    const fused = fuseRrf(leg("x", "y"), leg("y", "x"), SEARCH_DEPTH);
    expect(fused[0].score).toBe(fused[1].score);
    expect(order(fused)).toEqual(["y", "x"]);
    // The same tie with the IDs swapped: the semantic rank decides, not the ID.
    expect(order(fuseRrf(leg("y", "x"), leg("x", "y"), SEARCH_DEPTH))).toEqual(["x", "y"]);
  });

  it("lexical rank 1 with semantic rank 2 wins once the semantic top is lexical rank 3", () => {
    // "t": lexical 1, semantic 2. "s": semantic 1, lexical 3. "m": lexical 2 only.
    const fused = fuseRrf(leg("t", "m", "s"), leg("s", "t"), SEARCH_DEPTH);
    expect(order(fused)).toEqual(["t", "s", "m"]);
  });

  it("ties are decided by exact arithmetic, not by float rounding", () => {
    // 1/(60+a) + 1/(60+b) is symmetric in a and b, so every swapped pair is an
    // exact tie whatever the float sum rounds to; semantic rank must decide.
    for (const [a, b] of [[1, 50], [7, 31], [13, 44], [2, 3]] as const) {
      const lexical = ids("l", 50);
      const semantic = ids("s", 50);
      lexical[a - 1] = "p";
      semantic[b - 1] = "p"; // p: lexical a, semantic b
      lexical[b - 1] = "q";
      semantic[a - 1] = "q"; // q: lexical b, semantic a (the better semantic rank)
      const fused = fuseRrf(leg(...lexical), leg(...semantic), SEARCH_DEPTH);
      const rankOf = (id: string) => fused.find((r) => r.source_article_id === id)!.rank;
      expect({ a, b, first: rankOf("q") < rankOf("p") }).toEqual({ a, b, first: true });
      expect(rankOf("p") - rankOf("q")).toBe(1);
    }
  });

  it("takes title, url and category from the leg that has the article", () => {
    const lexical: SearchRow[] = [{ ...row(1, "1"), category: "Email" }];
    const semantic: SearchRow[] = [{ ...row(1, "2"), category: "Network" }];
    const fused = fuseRrf(lexical, semantic, SEARCH_DEPTH);
    expect(fused).toEqual([
      { rank: 1, source_article_id: "2", title: "Title 2", url: "https://example.test/kb/2",
        category: "Network", score: rrf(MISSING, 1) },
      { rank: 2, source_article_id: "1", title: "Title 1", url: "https://example.test/kb/1",
        category: "Email", score: rrf(1, MISSING) },
    ]);
  });

  it("does not change its inputs", () => {
    const lexical = leg("1", "2");
    const semantic = leg("2", "3");
    const before = JSON.stringify([lexical, semantic]);
    fuseRrf(lexical, semantic, SEARCH_DEPTH);
    expect(JSON.stringify([lexical, semantic])).toBe(before);
  });

  it("the legs are plain parameters: any two ranked lists fuse the same way", () => {
    // Nothing in the fusion knows which SQL function produced a leg.
    const a = leg("10", "20", "30");
    const b = leg("30", "10");
    expect(order(fuseRrf(a, b, SEARCH_DEPTH))).toEqual(["10", "30", "20"]);
    expect(order(fuseRrf(a, b, 3, 60))).toEqual(["10", "30", "20"]);
  });

  it.each([
    ["ranks not starting at 1", [row(2, "1")]],
    ["a gap in ranks", [row(1, "1"), row(3, "2")]],
    ["ranks out of order", [row(2, "1"), row(1, "2")]],
    ["a duplicate article", [row(1, "1"), row(2, "1")]],
    ["more rows than depth", leg(...ids("a", 51))],
  ])("rejects a leg with %s, as either leg", (_name, bad) => {
    expect(() => fuseRrf(bad, leg("9"), SEARCH_DEPTH)).toThrow(HybridLegError);
    expect(() => fuseRrf(leg("9"), bad, SEARCH_DEPTH)).toThrow(HybridLegError);
  });

  it.each([0, -1, 1.5, Number.NaN])("rejects %s as depth or as k", (bad) => {
    expect(() => fuseRrf(leg("1"), leg("1"), bad)).toThrow(HybridLegError);
    expect(() => fuseRrf(leg("1"), leg("1"), SEARCH_DEPTH, bad)).toThrow(HybridLegError);
  });

  it("the error carries a fixed code and nothing from the rows", () => {
    const bad = [row(1, "kbf-sentinel-7f3a9c"), row(2, "kbf-sentinel-7f3a9c")];
    let caught: unknown;
    try {
      fuseRrf(bad, [], SEARCH_DEPTH);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(HybridLegError);
    expect((caught as HybridLegError).code).toBe("HYBRID_LEG_INVALID");
    expect(String(caught)).not.toContain("sentinel");
  });
});

describe("makeHybridSearch", () => {
  it("calls each leg once with the query and depth, and fuses the two lists", async () => {
    const lexical = vi.fn<SearchFn>(async () => leg("5", "7"));
    const semantic = vi.fn<SearchFn>(async () => leg("7", "5"));
    const hybrid = makeHybridSearch({ lexical, semantic });
    const rows = await hybrid("printer offline", SEARCH_DEPTH);
    expect(order(rows)).toEqual(["7", "5"]);
    expect(lexical).toHaveBeenCalledTimes(1);
    expect(lexical).toHaveBeenCalledWith("printer offline", SEARCH_DEPTH);
    expect(semantic).toHaveBeenCalledTimes(1);
    expect(semantic).toHaveBeenCalledWith("printer offline", SEARCH_DEPTH);
  });

  it("starts the lexical leg before the semantic leg has finished", async () => {
    const events: string[] = [];
    let releaseSemantic: () => void = () => {};
    const semanticGate = new Promise<void>((resolve) => {
      releaseSemantic = resolve;
    });
    const semantic: SearchFn = async () => {
      events.push("semantic start");
      await semanticGate; // stands in for the query embedding call
      events.push("semantic end");
      return leg("1");
    };
    const lexical: SearchFn = async () => {
      events.push("lexical start");
      return leg("1");
    };
    const pending = makeHybridSearch({ lexical, semantic })("vpn drops", SEARCH_DEPTH);
    await Promise.resolve();
    expect(events).toEqual(["lexical start", "semantic start"]);
    releaseSemantic();
    await pending;
    expect(events).toEqual(["lexical start", "semantic start", "semantic end"]);
  });

  it("works for any pair of legs without a change to the fusion", async () => {
    // What a later hybrid over two other retrievers would pass in.
    const otherLexical = vi.fn<SearchFn>(async () => leg("30", "10"));
    const otherSemantic = vi.fn<SearchFn>(async () => leg("10", "20"));
    const rows = await makeHybridSearch({ lexical: otherLexical, semantic: otherSemantic })(
      "shared drive",
      SEARCH_DEPTH,
    );
    expect(order(rows)).toEqual(["10", "30", "20"]);
  });

  class LegError extends Error {
    constructor(
      message: string,
      readonly code: string,
    ) {
      super(message);
      this.name = "LegError";
    }
  }

  it("fails with the semantic leg's error when only that leg fails", async () => {
    const err = new LegError("x", "OPENAI_HTTP_429");
    const hybrid = makeHybridSearch({
      lexical: async () => leg("1"),
      semantic: async () => {
        throw err;
      },
    });
    await expect(hybrid("wifi", SEARCH_DEPTH)).rejects.toBe(err);
  });

  it("fails with the lexical leg's error when only that leg fails", async () => {
    const err = new LegError("x", "57014");
    const hybrid = makeHybridSearch({
      lexical: async () => {
        throw err;
      },
      semantic: async () => leg("1"),
    });
    await expect(hybrid("wifi", SEARCH_DEPTH)).rejects.toBe(err);
  });

  it("when both legs fail, reports the semantic error and leaves nothing unhandled", async () => {
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    try {
      const semanticErr = new LegError("x", "KBF04");
      const hybrid = makeHybridSearch({
        lexical: async () => {
          throw new LegError("x", "57014");
        },
        semantic: async () => {
          throw semanticErr;
        },
      });
      await expect(hybrid("wifi", SEARCH_DEPTH)).rejects.toBe(semanticErr);
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      process.off("unhandledRejection", unhandled);
    }
  });

  it("never returns one leg's list as a fallback", async () => {
    const lexical = vi.fn<SearchFn>(async () => leg("1", "2"));
    const hybrid = makeHybridSearch({
      lexical,
      semantic: async () => {
        throw new LegError("x", "OPENAI_KEY_MISSING");
      },
    });
    await expect(hybrid("wifi", SEARCH_DEPTH)).rejects.toMatchObject({ code: "OPENAI_KEY_MISSING" });
  });

  it("a malformed leg is a HybridLegError", async () => {
    const hybrid = makeHybridSearch({
      lexical: async () => [row(1, "1"), row(2, "1")],
      semantic: async () => leg("1"),
    });
    await expect(hybrid("wifi", SEARCH_DEPTH)).rejects.toBeInstanceOf(HybridLegError);
  });
});

describe("hybrid gating", () => {
  function fakes() {
    return {
      lexical: vi.fn<SearchFn>(async () => [row(1, "11", 0.5)]),
      semantic: vi.fn<SearchFn>(async () => [row(1, "22", 0.75)]),
      "lexical-v2": vi.fn<SearchFn>(async () => [row(1, "33", 0.25)]),
      "semantic-v2": vi.fn<SearchFn>(async () => [row(1, "44", 0.125)]),
      hybrid: vi.fn<SearchFn>(async () => [row(1, "55", 0.03125)]),
    };
  }

  async function post(mode: string, env: SearchEnv) {
    const f = fakes();
    const response = await handleSearch(jsonRequest({ query: "wifi", mode }), {
      retrievers: f,
      gate: () => "open" as const,
      limit: async () => "ok" as const,
      env,
    });
    return { f, status: response.status, body: await response.json() };
  }

  function expectNoneCalled(f: ReturnType<typeof fakes>) {
    for (const fn of Object.values(f)) expect(fn).not.toHaveBeenCalled();
  }

  it("is a known mode but not a possible default", () => {
    expect(SEARCH_MODES).toContain("hybrid");
    expect(DEFAULT_SEARCH_MODES).toEqual(["lexical", "semantic"]);
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "hybrid" })).toBeNull();
    expect(
      resolveDefaultMode({ DEFAULT_SEARCH_MODE: "hybrid", EVAL_MODES_ENABLED: "true" }),
    ).toBeNull();
  });

  it("is blocked unless EVAL_MODES_ENABLED is exactly true, whatever the default", async () => {
    for (const defaultMode of [undefined, "lexical", "semantic"]) {
      expect(isModeAllowed("hybrid", defaultMode ?? "lexical", {})).toBe(false);
      for (const value of [undefined, "false", "1", "TRUE", "yes", "", " true", "true "]) {
        const env: SearchEnv = {
          ...(defaultMode === undefined ? {} : { DEFAULT_SEARCH_MODE: defaultMode }),
          ...(value === undefined ? {} : { EVAL_MODES_ENABLED: value }),
        };
        const { f, status, body } = await post("hybrid", env);
        expect({ defaultMode, value, status, body })
          .toEqual({ defaultMode, value, status: 400, body: INVALID });
        expectNoneCalled(f);
      }
    }
  });

  it("with EVAL_MODES_ENABLED=true calls only the hybrid retriever", async () => {
    for (const defaultMode of [undefined, "lexical", "semantic"]) {
      const env: SearchEnv = {
        ...EVAL_ON,
        ...(defaultMode === undefined ? {} : { DEFAULT_SEARCH_MODE: defaultMode }),
      };
      expect(isModeAllowed("hybrid", defaultMode ?? "lexical", env)).toBe(true);
      const { f, status, body } = await post("hybrid", env);
      expect(status).toBe(200);
      expect(body).toEqual({
        mode: "hybrid",
        results: [{ rank: 1, sourceArticleId: "55", title: "Title 55",
          url: "https://example.test/kb/55", score: 0.03125 }],
      });
      expect(f.hybrid).toHaveBeenCalledTimes(1);
      expect(f.hybrid).toHaveBeenCalledWith("wifi", SEARCH_DEPTH);
      expect(f.lexical).not.toHaveBeenCalled();
      expect(f.semantic).not.toHaveBeenCalled();
      expect(f["lexical-v2"]).not.toHaveBeenCalled();
      expect(f["semantic-v2"]).not.toHaveBeenCalled();
    }
  });

  it("DEFAULT_SEARCH_MODE=hybrid is a config error, with or without eval modes", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    for (const env of [
      { DEFAULT_SEARCH_MODE: "hybrid" },
      { DEFAULT_SEARCH_MODE: "hybrid", EVAL_MODES_ENABLED: "true" },
    ]) {
      for (const mode of ["hybrid", "semantic", "lexical"]) {
        const { f, status, body } = await post(mode, env);
        expect({ mode, status, body }).toEqual({ mode, status: 500, body: FAILED });
        expectNoneCalled(f);
      }
    }
    expect(errors).toHaveBeenCalledWith("search config error: invalid DEFAULT_SEARCH_MODE");
  });

  it("existing defaults still resolve the same way", () => {
    expect(resolveDefaultMode({})).toBe("lexical");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "lexical" })).toBe("lexical");
    expect(resolveDefaultMode({ DEFAULT_SEARCH_MODE: "semantic" })).toBe("semantic");
  });

  it("the route maps hybrid to searchHybrid, built from the two frozen retrievers", () => {
    const root = process.cwd();
    const route = readFileSync(join(root, "app", "api", "search", "route.ts"), "utf8");
    expect(route).toContain("hybrid: searchHybrid,");
    expect(route.match(/searchHybrid/g)).toHaveLength(2); // import + mapping
    const source = readFileSync(join(root, "lib", "search", "hybrid.ts"), "utf8");
    expect(source).toContain("lexical: searchLexical,");
    expect(source).toContain("semantic: searchSemantic,");
    expect(source).not.toContain("V2");
    expect(source).not.toMatch(/\bselect\b|kb_search_/i); // no SQL of its own
  });
});

describe("hybrid through handleSearch", () => {
  const SENTINEL = "kbf-sentinel-7f3a9c";
  const query = `my laptop ${SENTINEL} will not connect`;

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

  const never = vi.fn<SearchFn>(async () => {
    throw new Error("another retriever called");
  });

  async function run(hybrid: SearchFn, text = query) {
    const response = await handleSearch(jsonRequest({ query: text, mode: "hybrid" }), {
      retrievers: {
        lexical: never,
        semantic: never,
        "lexical-v2": never,
        "semantic-v2": never,
        hybrid,
      },
      gate: () => "open" as const,
      limit: async () => "ok" as const,
      env: EVAL_ON,
    });
    return { status: response.status, text: await response.text() };
  }

  it("returns the fused list as mode hybrid", async () => {
    const hybrid = makeHybridSearch({
      lexical: async () => leg("5", "7"),
      semantic: async () => leg("7", "5"),
    });
    const result = await run(hybrid, "printer offline");
    expect(result.status).toBe(200);
    expect(JSON.parse(result.text)).toEqual({
      mode: "hybrid",
      results: [
        { rank: 1, sourceArticleId: "7", title: "Title 7",
          url: "https://example.test/kb/7", score: rrf(2, 1) },
        { rank: 2, sourceArticleId: "5", title: "Title 5",
          url: "https://example.test/kb/5", score: rrf(1, 2) },
      ],
    });
    expect(never).not.toHaveBeenCalled();
  });

  const failing: [string, SearchFn, SearchFn][] = [
    [
      "success",
      async () => leg("1"),
      async () => leg("2"),
    ],
    [
      "lexical leg error echoing the query",
      async () => {
        throw new DatabaseError(`syntax error in tsquery: "${query}"`, "42601");
      },
      async () => leg("2"),
    ],
    [
      "semantic leg error with the query as its code",
      async () => leg("1"),
      async () => {
        throw new DatabaseError(query, query);
      },
    ],
    [
      "both legs failing with the query in their messages",
      async () => {
        throw new DatabaseError(query, "57014");
      },
      async () => {
        throw new DatabaseError(query, "KBF04");
      },
    ],
    [
      "a malformed leg",
      async () => [row(1, SENTINEL), row(2, SENTINEL)],
      async () => leg("2"),
    ],
  ];

  it.each(failing)("query text never leaks: %s", async (_name, lexical, semantic) => {
    const captured = captureOutput();
    const result = await run(makeHybridSearch({ lexical, semantic }));
    vi.restoreAllMocks();
    if (result.status !== 200) expect(JSON.parse(result.text)).toEqual(FAILED);
    expect(result.text).not.toContain("will not connect");
    for (const line of captured) expect(line).not.toContain(SENTINEL);
  });

  it("a leg failure is a 500 logged as that leg's class and code", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await run(
      makeHybridSearch({
        lexical: async () => leg("1"),
        semantic: async () => {
          throw new DatabaseError("embeddings incomplete", "KBF04");
        },
      }),
    );
    expect(result).toEqual({ status: 500, text: JSON.stringify(FAILED) });
    expect(errors.mock.calls.map((args) => format(...args))).toEqual([
      "search failed: DatabaseError KBF04",
    ]);
  });
});
