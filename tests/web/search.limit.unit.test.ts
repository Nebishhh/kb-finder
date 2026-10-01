import { describe, expect, it, vi } from "vitest";

import { DAILY_SEARCH_CAP, GLOBAL_DAY_KIND, GLOBAL_KEY } from "@/lib/limits/config";
import {
  DAILY_CAP_SQL,
  LimitError,
  makeDailyCap,
  type LimitQueryFn,
} from "@/lib/limits/daily-cap";
import {
  handleSearch,
  resolveRateLimit,
  type LimitFn,
  type SearchEnv,
  type SearchFn,
} from "@/lib/search/handler";
import type { SearchRow } from "@/lib/search/types";

// Offline. The limiter and the retrievers are fakes; the real SQL is exercised
// against a throwaway schema in search.db.test.ts.

const INVALID = { error: "Invalid request" };
const FAILED = { error: "Search failed" };
const TOO_LARGE = { error: "Request too large" };
const TOO_MANY = { error: "Too many requests" };

function jsonRequest(body: unknown): Request {
  return new Request("http://localhost/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const ROW: SearchRow = {
  rank: 1,
  source_article_id: "4778",
  title: "Title",
  url: "https://example.test/kb/4778",
  category: null,
  score: 0.5,
};

// Records the order of limiter and retriever calls.
function harness(limitResult: LimitFn = async () => "ok") {
  const calls: string[] = [];
  const limit = vi.fn<LimitFn>(async () => {
    calls.push("limit");
    return limitResult();
  });
  const lexical = vi.fn<SearchFn>(async () => {
    calls.push("lexical");
    return [ROW];
  });
  const semantic = vi.fn<SearchFn>(async () => {
    calls.push("semantic");
    return [ROW];
  });
  async function run(request: Request, env: SearchEnv = {}) {
    const response = await handleSearch(request, {
      retrievers: { lexical, semantic },
      gate: () => "open",
      limit,
      env,
    });
    return {
      status: response.status,
      cache: response.headers.get("Cache-Control"),
      body: await response.json(),
    };
  }
  return { calls, limit, lexical, semantic, run };
}

describe("which requests reach the limiter", () => {
  it.each([
    ["schema-invalid (400)", jsonRequest({ query: "", mode: "lexical" }), 400, INVALID],
    ["disallowed mode (400)", jsonRequest({ query: "wifi", mode: "semantic" }), 400, INVALID],
    [
      "over the byte cap (413)",
      jsonRequest({ query: "x".repeat(40_000), mode: "lexical" }),
      413,
      TOO_LARGE,
    ],
  ])("%s never calls the limiter", async (_name, request, status, body) => {
    const h = harness();
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await h.run(request);
    expect(result).toEqual({ status, cache: "no-store", body });
    expect(h.limit).not.toHaveBeenCalled();
    expect(h.calls).toEqual([]);
    errors.mockRestore();
  });

  it("a valid, allowed search calls the limiter once, before the retriever", async () => {
    const h = harness();
    const result = await h.run(jsonRequest({ query: "wifi", mode: "lexical" }));
    expect(result.status).toBe(200);
    expect(h.calls).toEqual(["limit", "lexical"]);
  });

  it("a retriever failure has already been counted", async () => {
    const h = harness();
    h.lexical.mockImplementationOnce(async () => {
      h.calls.push("lexical");
      throw new Error("db down");
    });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await h.run(jsonRequest({ query: "wifi", mode: "lexical" }));
    expect(result).toEqual({ status: 500, cache: "no-store", body: FAILED });
    expect(h.calls).toEqual(["limit", "lexical"]);
    errors.mockRestore();
  });

  it("the cap applies to semantic too", async () => {
    const h = harness(async () => "daily_cap");
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await h.run(jsonRequest({ query: "wifi", mode: "semantic" }), {
      EVAL_MODES_ENABLED: "true",
    });
    expect(result).toEqual({ status: 429, cache: "no-store", body: TOO_MANY });
    expect(h.semantic).not.toHaveBeenCalled();
    errors.mockRestore();
  });
});

describe("limiter outcomes", () => {
  it("over the cap: 429 with a fixed body, retriever never called, fixed log line", async () => {
    const h = harness(async () => "daily_cap");
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await h.run(jsonRequest({ query: "wifi", mode: "lexical" }));
    expect(result).toEqual({ status: 429, cache: "no-store", body: TOO_MANY });
    expect(h.lexical).not.toHaveBeenCalled();
    expect(errors.mock.calls).toEqual([["search rejected: DAILY_CAP"]]);
    errors.mockRestore();
  });

  it("limiter error: 500 with the existing fixed body (fail closed)", async () => {
    class DatabaseError extends Error {
      readonly code = "57014";
    }
    const h = harness(async () => {
      throw new DatabaseError("canceling statement due to statement timeout");
    });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await h.run(jsonRequest({ query: "wifi", mode: "lexical" }));
    expect(result).toEqual({ status: 500, cache: "no-store", body: FAILED });
    expect(h.lexical).not.toHaveBeenCalled();
    expect(errors.mock.calls).toEqual([["search limit failed: DatabaseError 57014"]]);
    errors.mockRestore();
  });
});

describe("RATE_LIMIT=off", () => {
  it("resolves exactly 'off', and only off Vercel", () => {
    expect(resolveRateLimit({})).toBe("on");
    expect(resolveRateLimit({ RATE_LIMIT: "OFF" })).toBe("on");
    expect(resolveRateLimit({ RATE_LIMIT: "0" })).toBe("on");
    expect(resolveRateLimit({ RATE_LIMIT: "off" })).toBe("skip");
    expect(resolveRateLimit({ RATE_LIMIT: "off", VERCEL: "" })).toBe("skip");
    expect(resolveRateLimit({ RATE_LIMIT: "off", VERCEL: "1" })).toBe("invalid");
    expect(resolveRateLimit({ VERCEL: "1" })).toBe("on");
  });

  it("without VERCEL: the limiter is skipped and the search runs", async () => {
    const h = harness(async () => "daily_cap");
    const result = await h.run(jsonRequest({ query: "wifi", mode: "lexical" }), {
      RATE_LIMIT: "off",
    });
    expect(result.status).toBe(200);
    expect(h.calls).toEqual(["lexical"]);
  });

  it.each([
    ["a valid search", jsonRequest({ query: "wifi", mode: "lexical" })],
    ["an invalid request", jsonRequest({ query: "", mode: "lexical" })],
  ])("with VERCEL set: %s gets 500 and nothing runs", async (_name, request) => {
    const h = harness();
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await h.run(request, { RATE_LIMIT: "off", VERCEL: "1" });
    expect(result).toEqual({ status: 500, cache: "no-store", body: FAILED });
    expect(h.calls).toEqual([]);
    expect(errors.mock.calls).toEqual([["search config error: RATE_LIMIT=off on Vercel"]]);
    errors.mockRestore();
  });

  it("with VERCEL set and the limiter on, searches run normally", async () => {
    const h = harness();
    const result = await h.run(jsonRequest({ query: "wifi", mode: "lexical" }), {
      VERCEL: "1",
    });
    expect(result.status).toBe(200);
    expect(h.calls).toEqual(["limit", "lexical"]);
  });
});

describe("makeDailyCap", () => {
  const NOW = new Date("2026-10-01T15:30:00.000Z");

  function fakeQuery(count: unknown, rows = 1) {
    return vi.fn<LimitQueryFn>(async () => ({
      rows: Array.from({ length: rows }, () => ({ count })),
    }));
  }

  it("is 200 per UTC day on the global row", () => {
    expect(DAILY_SEARCH_CAP).toBe(200);
    expect(GLOBAL_DAY_KIND).toBe("search_global_day");
    expect(GLOBAL_KEY).toBe("all");
  });

  it("sends one statement with the time, kind and key as parameters", async () => {
    const query = fakeQuery(1);
    await makeDailyCap({ query, now: () => NOW })();
    expect(query).toHaveBeenCalledWith(DAILY_CAP_SQL, [
      "2026-10-01T15:30:00.000Z",
      "search_global_day",
      "all",
    ]);
    expect(DAILY_CAP_SQL).not.toContain(";");
    expect(DAILY_CAP_SQL).not.toMatch(/now\(\)/i);
  });

  it.each([
    [1, "ok"],
    [199, "ok"],
    [200, "ok"],
    [201, "daily_cap"],
    [5000, "daily_cap"],
  ])("count %i -> %s", async (count, expected) => {
    expect(await makeDailyCap({ query: fakeQuery(count), now: () => NOW })()).toBe(expected);
  });

  it.each([
    ["no row", fakeQuery(1, 0)],
    ["two rows", fakeQuery(1, 2)],
    ["a string count", fakeQuery("5")],
    ["a fractional count", fakeQuery(1.5)],
    ["a zero count", fakeQuery(0)],
    ["a null count", fakeQuery(null)],
  ])("%s throws LimitError", async (_name, query) => {
    await expect(makeDailyCap({ query, now: () => NOW })()).rejects.toBeInstanceOf(LimitError);
  });

  it("passes database errors through to the handler", async () => {
    const query = vi.fn<LimitQueryFn>(async () => {
      throw new Error("connection refused");
    });
    await expect(makeDailyCap({ query, now: () => NOW })()).rejects.toThrow("connection refused");
  });
});
