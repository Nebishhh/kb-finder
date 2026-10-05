import { readFileSync } from "node:fs";
import { join } from "node:path";

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import Home, { dynamic } from "@/app/page";
import SearchClient, {
  runSearch,
  SearchOutput,
  shouldSearchOnKey,
  statusText,
  type FetchFn,
  type SearchOutcome,
} from "@/app/search-client";
import type { SearchResult } from "@/lib/search/handler";

// Offline: no DOM and no real fetch. The server page is called directly; the
// client's request and rendering are tested through runSearch and SearchOutput.

const ROOT = join(__dirname, "..", "..");
const QUERY = "wifi keeps dropping in the library";
const NBSP = String.fromCharCode(0xa0);

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

// The visible text of rendered markup, tags removed.
function text(html: string): string {
  return html.replace(/<[^>]*>/g, "");
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
    expect(html).toContain("<h1>From a messy issue to the closest article.</h1>");
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
  it("heads the results with the shown count, without the mode, a count line or scores", () => {
    const results = [result(1), result(2), result(3)];
    const html = render({ ok: true, data: { mode: "semantic", results } });
    expect(html).toContain(">Top 3 matches<");
    expect(html).toContain(
      ">Ranked by closeness. Results always appear, so check that the top few fit.<",
    );
    expect(html).toContain("Article 1");
    expect(html).not.toContain("Mode:");
    expect(html).not.toContain("semantic");
    expect(html).not.toContain("Showing");
    expect(html).not.toMatch(/score/i);
    expect(html).not.toContain(String(result(2).score));
    expect(html).not.toContain(String(result(3).score));
  });

  it("one result: Top match", () => {
    const html = render({ ok: true, data: { mode: "lexical", results: [result(1)] } });
    expect(html).toContain(">Top match<");
    expect(html).not.toContain("matches");
  });

  it("shows the first 10 of up to 50 results", () => {
    const results = Array.from({ length: 50 }, (_, i) => result(i + 1));
    const html = render({ ok: true, data: { mode: "lexical", results } });
    expect(html).toContain(">Top 10 matches<");
    expect(html.match(/<li>/g)).toHaveLength(10);
    expect(html).toContain("Article 10<");
    expect(html).not.toContain("Article 11<");
  });

  it("each title links to its article in a new tab, with the category below", () => {
    const html = render({
      ok: true,
      data: { mode: "lexical", results: [{ ...result(1), category: "Wireless" }, result(2)] },
    });
    expect(html).toContain(
      `<a href="https://example.test/kb/1001" target="_blank" rel="noopener noreferrer">Article 1<span class="nowrap">${NBSP}<svg`,
    );
    expect(html.match(/<svg class="arrow" aria-hidden="true"/g)).toHaveLength(2);
    expect(
      html.match(/<span class="visually-hidden"> \(opens in a new tab\)<\/span><\/span><\/a>/g),
    ).toHaveLength(2);
    expect(html.match(/class="category"/g)).toHaveLength(1);
    expect(html).toContain('<p class="category">Wireless</p>');
  });

  it("no results: a fixed empty state ending in a tick, with no warning mark", () => {
    const html = render({ ok: true, data: { mode: "lexical", results: [] } });
    expect(html).toContain(">No matching articles.<");
    expect(html).toContain(">Try describing the problem in different words.<");
    expect(html).toContain('class="tick"');
    expect(html).not.toContain("warn-mark");
    expect(html).not.toContain("<ol");
    expect(html).not.toContain("Top");
  });

  it.each([
    ["expired", "Your session has expired. Sign in again."],
    ["too_long", "The description is too long. Please shorten it."],
    ["daily_limit", "The daily search limit has been reached. Please try again tomorrow."],
    ["failed", "Search failed. Please try again."],
  ] as const)("%s: only the fixed text, beside the warning mark", (failure, message) => {
    const html = render({ ok: false, failure });
    expect(text(html)).toBe(message);
    expect(html).toContain('<svg class="warn-mark" aria-hidden="true"');
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
    const html = render({ ok: false, failure: "failed" });
    expect(text(html)).toBe("Search failed. Please try again.");
    expect(html).toContain(">Search failed. Please try again.</p>");
  });

  it("no error message links anywhere but /gate", () => {
    for (const failure of ["too_long", "daily_limit", "failed"] as const) {
      expect(render({ ok: false, failure })).not.toContain("<a");
    }
  });
});

describe("search page", () => {
  function renderClient(mode: string): string {
    return renderToStaticMarkup(createElement(SearchClient, { mode }));
  }

  it.each([
    ["lexical", "Keyword search"],
    ["semantic", "Semantic search"],
    ["hybrid", "Hybrid search"],
  ])("the header names mode %s as %s", (mode, label) => {
    expect(renderClient(mode)).toContain(`<span class="mode-label">${label}</span>`);
  });

  it("shows the approved copy and a disabled Find articles button, with no form", () => {
    const html = renderClient("lexical");
    expect(html).toContain('<span class="brand">KB Finder</span>');
    expect(html).toContain("<h1>From a messy issue to the closest article.</h1>");
    expect(html).toContain(">Help articles use IT terms. You don&#x27;t have to.<");
    expect(html).toContain(
      ">Searches 3,109 public articles from the Coyote OneStop Knowledge Base.<",
    );
    expect(html).toContain('<label class="field-label" for="issue">What&#x27;s going wrong?</label>');
    expect(html).not.toContain("placeholder");
    expect(html).toContain('aria-describedby="issue-help"');
    expect(html).toContain(
      '<p id="issue-help" class="helper">Leave out names, IDs, emails and phone numbers.</p>',
    );
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Find articles<\/button>/);
    expect(html).not.toContain("<form");
  });

  it("the textarea hints search on the Enter key", () => {
    expect(renderClient("lexical")).toMatch(/<textarea[^>]*enterKeyHint="search"/);
  });
});

describe("shouldSearchOnKey", () => {
  const enter = { key: "Enter", shiftKey: false, isComposing: false };

  it("Enter searches", () => {
    expect(shouldSearchOnKey(enter, false, "vpn drops")).toBe(true);
  });

  it("Shift+Enter does not (it inserts a newline)", () => {
    expect(shouldSearchOnKey({ ...enter, shiftKey: true }, false, "vpn drops")).toBe(false);
  });

  it("nothing happens during an IME composition", () => {
    expect(shouldSearchOnKey({ ...enter, isComposing: true }, false, "vpn drops")).toBe(false);
    expect(shouldSearchOnKey({ ...enter, key: "Process" }, false, "vpn drops")).toBe(false);
  });

  it("not while loading, and not with an empty query (the button's condition)", () => {
    expect(shouldSearchOnKey(enter, true, "vpn drops")).toBe(false);
    expect(shouldSearchOnKey(enter, false, "")).toBe(false);
  });

  it("other keys do not search", () => {
    expect(shouldSearchOnKey({ ...enter, key: "a" }, false, "vpn drops")).toBe(false);
  });
});

describe("status line", () => {
  it("is one empty hidden status element before any search; the output has no live region", () => {
    const html = renderToStaticMarkup(createElement(SearchClient, { mode: "lexical" }));
    expect(html.match(/role="status"/g)).toHaveLength(1);
    expect(html).toContain('<p class="visually-hidden" role="status"></p>');
    expect(html).toContain('<div class="output"></div>');
    expect(html).not.toContain("aria-live");
  });

  it("is empty initially", () => {
    expect(statusText(false, null)).toBe("");
  });

  it("while loading: the matching line, even with an old outcome", () => {
    expect(statusText(true, null)).toBe("Matching against 3,109 articles.");
    expect(statusText(true, { ok: false, failure: "failed" })).toBe(
      "Matching against 3,109 articles.",
    );
  });

  it("with results: the same heading text", () => {
    const one = { mode: "lexical", results: [result(1)] };
    const many = { mode: "lexical", results: Array.from({ length: 50 }, (_, i) => result(i + 1)) };
    const three = { mode: "lexical", results: [result(1), result(2), result(3)] };
    expect(statusText(false, { ok: true, data: one })).toBe("Top match");
    expect(statusText(false, { ok: true, data: three })).toBe("Top 3 matches");
    expect(statusText(false, { ok: true, data: many })).toBe("Top 10 matches");
    for (const data of [one, three, many]) {
      const heading = statusText(false, { ok: true, data });
      expect(render({ ok: true, data })).toContain(`<h2 id="results-heading">${heading}</h2>`);
    }
  });

  it("with no results: the empty message", () => {
    const outcome: SearchOutcome = { ok: true, data: { mode: "lexical", results: [] } };
    expect(statusText(false, outcome)).toBe("No matching articles.");
    expect(render(outcome)).toContain(`<p>${statusText(false, outcome)}</p>`);
  });

  it.each(["expired", "too_long", "daily_limit", "failed"] as const)(
    "on %s: the same fixed message as shown",
    (failure) => {
      const outcome: SearchOutcome = { ok: false, failure };
      expect(statusText(false, outcome)).toBe(text(render(outcome)));
    },
  );
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
