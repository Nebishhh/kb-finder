import { readFileSync } from "node:fs";
import { join } from "node:path";

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import Home, { dynamic } from "@/app/page";
import SearchClient, {
  runSearch,
  SearchOutput,
  type FetchFn,
  type SearchOutcome,
} from "@/app/search-client";
import type { SearchResult } from "@/lib/search/handler";

// Offline: no DOM and no real fetch. The server page is called directly; the
// client's request and rendering are tested through runSearch and SearchOutput.

const ROOT = join(__dirname, "..", "..");
const QUERY = "wifi keeps dropping in the library";

function result(n: number): SearchResult {
  return {
    rank: n,
    sourceArticleId: String(1000 + n),
    title: `Article ${n}`,
    url: `https://example.test/kb/${1000 + n}`,
    score: 1 / n,
  };
}

function render(outcome: SearchOutcome): string {
  return renderToStaticMarkup(createElement(SearchOutput, { outcome }));
}

type Call = { input: string; init: RequestInit };

function fakeFetch(response: Response): { fetchFn: FetchFn; calls: Call[] } {
  const calls: Call[] = [];
  const fetchFn: FetchFn = async (input, init) => {
    calls.push({ input, init });
    return response;
  };
  return { fetchFn, calls };
}

describe("server page", () => {
  it("is rendered per request", () => {
    expect(dynamic).toBe("force-dynamic");
  });

  it("passes lexical to the client when DEFAULT_SEARCH_MODE is unset", () => {
    const page = Home() as ReactElement<{ mode: string }>;
    expect(page.type).toBe(SearchClient);
    expect(page.props.mode).toBe("lexical");
  });

  it("passes the configured default mode to the client", () => {
    process.env.DEFAULT_SEARCH_MODE = "semantic";
    const page = Home() as ReactElement<{ mode: string }>;
    expect(page.type).toBe(SearchClient);
    expect(page.props.mode).toBe("semantic");
  });

  it("renders a fixed configuration error when the default mode is invalid", () => {
    process.env.DEFAULT_SEARCH_MODE = "hybrid";
    const page = Home();
    expect(page.type).not.toBe(SearchClient);
    const html = renderToStaticMarkup(page);
    expect(html).toContain("Configuration error: the default search mode is not valid.");
    expect(html).not.toContain("hybrid");
    expect(html).not.toContain("<textarea");
  });
});

describe("runSearch", () => {
  it("posts exactly {query, mode} to /api/search, uncached", async () => {
    const { fetchFn, calls } = fakeFetch(
      Response.json({ mode: "semantic", results: [] }),
    );
    await runSearch(fetchFn, QUERY, "semantic");
    expect(calls).toHaveLength(1);
    expect(calls[0].input).toBe("/api/search");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.cache).toBe("no-store");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({
      query: QUERY,
      mode: "semantic",
    });
  });

  it("returns the response on 200", async () => {
    const data = { mode: "lexical", results: [result(1)] };
    const { fetchFn } = fakeFetch(Response.json(data));
    expect(await runSearch(fetchFn, QUERY, "lexical")).toEqual({ ok: true, data });
  });

  it.each([
    [401, "expired"],
    [413, "too_long"],
    [429, "daily_limit"],
    [400, "failed"],
    [500, "failed"],
  ])("maps status %i to %s", async (status, failure) => {
    const { fetchFn } = fakeFetch(
      Response.json({ error: "server text" }, { status }),
    );
    expect(await runSearch(fetchFn, QUERY, "lexical")).toEqual({ ok: false, failure });
  });

  it("maps a network error to the generic failure", async () => {
    const fetchFn: FetchFn = () => Promise.reject(new Error("offline"));
    expect(await runSearch(fetchFn, QUERY, "lexical")).toEqual({
      ok: false,
      failure: "failed",
    });
  });
});

describe("SearchOutput", () => {
  it("shows the mode from the response", () => {
    const html = render({ ok: true, data: { mode: "semantic", results: [result(1)] } });
    expect(html).toContain("Mode: semantic");
    expect(html).toContain("Article 1");
  });

  it("shows the first 10 of up to 50 results", () => {
    const results = Array.from({ length: 50 }, (_, i) => result(i + 1));
    const html = render({ ok: true, data: { mode: "lexical", results } });
    expect(html).toContain("Showing 10 of 50 results.");
    expect(html.match(/<li>/g)).toHaveLength(10);
    expect(html).toContain("Article 10<");
    expect(html).not.toContain("Article 11<");
  });

  it("401: session expired, with a link to /gate", () => {
    const html = render({ ok: false, failure: "expired" });
    expect(html).toContain("Your session has expired.");
    expect(html).toContain('<a href="/gate">');
  });

  it("413: the description is too long", () => {
    expect(render({ ok: false, failure: "too_long" })).toContain(
      "The description is too long.",
    );
  });

  it("429: the daily search limit was reached", () => {
    expect(render({ ok: false, failure: "daily_limit" })).toContain(
      "The daily search limit has been reached.",
    );
  });

  it("anything else: the generic error", () => {
    expect(render({ ok: false, failure: "failed" })).toBe(
      "<p>Search failed. Please try again.</p>",
    );
  });

  it("no error message links anywhere but /gate", () => {
    for (const failure of ["too_long", "daily_limit", "failed"] as const) {
      expect(render({ ok: false, failure })).not.toContain("<a");
    }
  });
});

describe("privacy", () => {
  it("the search client keeps the query out of URLs, storage and logs", () => {
    const source = readFileSync(join(ROOT, "app", "search-client.tsx"), "utf8");
    expect(source).not.toMatch(
      /localStorage|sessionStorage|indexedDB|document\.cookie|console\.|searchParams|URLSearchParams|useRouter|history\./,
    );
  });

  it("the client starts empty, with no results or error shown", () => {
    const html = renderToStaticMarkup(createElement(SearchClient, { mode: "lexical" }));
    expect(html).toContain("<textarea");
    expect(html).not.toContain("Mode:");
    expect(html).not.toMatch(/expired|too long|limit|failed/i);
  });
});
