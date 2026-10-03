"use client";

import { useState } from "react";

import type { SearchResponse } from "@/lib/search/handler";

// The API returns up to 50 results; the UI shows the first 10 (SPEC.md).
const DISPLAY_COUNT = 10;

// Every error message is fixed: no response text is ever shown.
export type SearchFailure = "expired" | "too_long" | "daily_limit" | "failed";

export type SearchOutcome =
  | { ok: true; data: SearchResponse }
  | { ok: false; failure: SearchFailure };

export type FetchFn = (input: string, init: RequestInit) => Promise<Response>;

function failureFor(status: number): SearchFailure {
  if (status === 401) return "expired";
  if (status === 413) return "too_long";
  if (status === 429) return "daily_limit";
  return "failed";
}

// The query goes only into the POST body: never a URL, storage or a log.
export async function runSearch(
  fetchFn: FetchFn,
  query: string,
  mode: string,
): Promise<SearchOutcome> {
  try {
    const response = await fetchFn("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, mode }),
      cache: "no-store",
    });
    if (!response.ok) return { ok: false, failure: failureFor(response.status) };
    return { ok: true, data: (await response.json()) as SearchResponse };
  } catch {
    return { ok: false, failure: "failed" };
  }
}

export function SearchOutput({ outcome }: { outcome: SearchOutcome }) {
  if (!outcome.ok) {
    switch (outcome.failure) {
      case "expired":
        return (
          <p>
            Your session has expired. <a href="/gate">Sign in again</a>.
          </p>
        );
      case "too_long":
        return <p>The description is too long. Please shorten it.</p>;
      case "daily_limit":
        return <p>The daily search limit has been reached. Please try again tomorrow.</p>;
      case "failed":
        return <p>Search failed. Please try again.</p>;
    }
  }

  const { mode, results } = outcome.data;
  const shown = results.slice(0, DISPLAY_COUNT);
  return (
    <>
      <p>Mode: {mode}</p>
      <p>
        Showing {shown.length} of {results.length} results.
      </p>
      <ol>
        {shown.map((result) => (
          <li key={result.sourceArticleId}>
            <a href={result.url} target="_blank" rel="noopener noreferrer">
              {result.title}
            </a>
            {result.category !== undefined && <> ({result.category})</>}
            {" "}score {result.score}
          </li>
        ))}
      </ol>
    </>
  );
}

export default function SearchClient({ mode }: { mode: string }) {
  // The query lives only in component state: never in the URL, storage or logs.
  const [query, setQuery] = useState("");
  const [outcome, setOutcome] = useState<SearchOutcome | null>(null);
  const [loading, setLoading] = useState(false);

  async function search() {
    setLoading(true);
    setOutcome(await runSearch(fetch, query, mode));
    setLoading(false);
  }

  return (
    <main>
      <h1>KB Finder</h1>
      <textarea
        aria-label="Describe the IT issue"
        rows={6}
        cols={80}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        autoComplete="off"
        spellCheck={false}
      />
      <div>
        <button
          type="button"
          onClick={search}
          disabled={loading || query.length === 0}
        >
          Search
        </button>
      </div>

      {outcome !== null && <SearchOutput outcome={outcome} />}
    </main>
  );
}
